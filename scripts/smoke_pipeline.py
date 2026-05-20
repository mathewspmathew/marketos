"""
scripts/smoke_pipeline.py

End-to-end smoke test for the discovery → scrape → verify → stats → pricing
pipeline. Run from the repo root:

  uv run python scripts/smoke_pipeline.py [shopify_product_id]

If a product id is provided, the test runs against that product. Otherwise
the first ShopifyProduct in the DB is picked. Each module's output is
printed with a clear delimiter so you can spot which step fails.

This does NOT enqueue Celery tasks — it invokes the functions inline so we
can read return values and DB state directly. Best run with workers stopped
so concurrent fan-outs don't interfere.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Ensure repo root is on the path.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text

from services.common.db import get_db
from services.common import models


GREEN = "\033[92m"
RED   = "\033[91m"
YEL   = "\033[93m"
BOLD  = "\033[1m"
RST   = "\033[0m"


def banner(title: str) -> None:
    print(f"\n{BOLD}─── {title} ───{RST}")


def ok(msg: str) -> None:
    print(f"{GREEN}✓ {msg}{RST}")


def fail(msg: str) -> None:
    print(f"{RED}✗ {msg}{RST}")


def warn(msg: str) -> None:
    print(f"{YEL}! {msg}{RST}")


# ─────────────────────────────────────────────────────────────────────────────
# 0. External API smoke tests (no DB)
# ─────────────────────────────────────────────────────────────────────────────
def test_serper() -> bool:
    banner("0a. Serper API")
    if not os.getenv("SERPER_API_KEY"):
        fail("SERPER_API_KEY not set — discovery will be unable to search.")
        return False
    try:
        from services.discovery_svc.serper import search_web, search_shopping
        web = search_web("nike air max 90 blue mens", num=5)
        shop = search_shopping("nike air max 90 blue mens", num=5)
        ok(f"search_web returned {len(web)} hits")
        if web:
            print(f"   sample: {web[0].domain} — {web[0].title[:60]}")
        ok(f"search_shopping returned {len(shop)} hits")
        if shop:
            print(f"   sample: {shop[0].domain} — price={shop[0].price}")
        return bool(web or shop)
    except Exception as exc:
        fail(f"Serper call raised: {type(exc).__name__}: {exc}")
        return False


def test_vertex() -> bool:
    banner("0b. Vertex text embedding")
    try:
        from services.common.vertex_embed import embed_text
        vec = embed_text("nike air max 90 blue", task_type="RETRIEVAL_DOCUMENT")
        if vec and len(vec) == 768:
            ok(f"embed_text returned 768-D vector; |v|≈{sum(x*x for x in vec)**0.5:.3f}")
            return True
        fail(f"embed_text returned unexpected: type={type(vec).__name__} len={len(vec) if vec else 0}")
        return False
    except Exception as exc:
        fail(f"Vertex call raised: {type(exc).__name__}: {exc}")
        return False


def test_groq_search_query() -> bool:
    banner("0c. Groq searchQuery generator")
    try:
        from services.scraper_svc.semantics import _groq_search_query
        q = _groq_search_query(
            title="Nike Air Max 90 Blue Mens Running Shoe",
            vendor="Nike",
            category="footwear",
            description="Classic Air Max sneaker in blue colourway",
        )
        if q:
            ok(f"search query generated: {q!r}")
            return True
        fail("Groq returned None/empty.")
        return False
    except Exception as exc:
        fail(f"Groq call raised: {type(exc).__name__}: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 1. Pick a product
# ─────────────────────────────────────────────────────────────────────────────
def pick_product(product_id: str | None) -> models.ShopifyProduct | None:
    banner("1. Pick a product")
    with get_db() as db:
        if product_id:
            p = db.get(models.ShopifyProduct, product_id)
            if not p:
                fail(f"Product {product_id} not found.")
                return None
        else:
            p = db.query(models.ShopifyProduct).first()
            if not p:
                fail("No ShopifyProduct rows in DB.")
                return None
        db.expunge(p)
    ok(f"product: {p.id}")
    print(f"   title: {p.title}")
    print(f"   searchQuery:         {p.searchQuery!r}")
    print(f"   searchQueryOverride: {p.searchQueryOverride!r}")
    print(f"   dynamicPricingEnabled: {p.dynamicPricingEnabled}")
    return p


# ─────────────────────────────────────────────────────────────────────────────
# 2. Verify the cached query vector exists (move-to-ShopifyProductEmbedding check)
# ─────────────────────────────────────────────────────────────────────────────
def check_query_vector(product_id: str) -> bool:
    banner("2. ShopifyProductEmbedding.searchQueryVector")
    from services.common.vertex_embed import load_search_query_vector
    with get_db() as db:
        v = load_search_query_vector(db, product_id)
    if v:
        ok(f"vector cached ({len(v)}-D); norm≈{sum(x*x for x in v)**0.5:.3f}")
        return True
    warn("no cached vector. Discovery will fall through to LLM-only arbitration.")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 3. Run discovery inline (no Celery)
# ─────────────────────────────────────────────────────────────────────────────
def run_discovery(product_id: str) -> int:
    banner("3. discovery.discover_competitors (inline)")
    from services.discovery_svc.main import discover_competitors
    try:
        # The task is bound; pass a dummy self via .run().
        result = discover_competitors.run(product_id)
        ok(f"returned: {result}")
        candidates = int(result.get("candidates", 0)) if isinstance(result, dict) else 0
        return candidates
    except Exception as exc:
        fail(f"discovery raised: {type(exc).__name__}: {exc}")
        return 0


def list_candidates(product_id: str, limit: int = 5) -> list[str]:
    banner("3b. CompetitorCandidate rows")
    with get_db() as db:
        rows = db.execute(
            text("""
                SELECT id, url, status, "embedScore", "rerankScore", "rerankReason"
                FROM "CompetitorCandidate"
                WHERE "shopifyProductId" = :id
                ORDER BY "discoveredAt" DESC
                LIMIT :lim
            """),
            {"id": product_id, "lim": limit},
        ).all()
    if not rows:
        warn("no candidates persisted.")
        return []
    for r in rows:
        print(f"   [{r.status:9s}] embed={r.embedScore} rerank={r.rerankScore} "
              f"reason={(r.rerankReason or '')[:40]:40s} {r.url[:60]}")
    return [r.id for r in rows if r.status in ("PENDING", "VERIFIED")]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Scrape one candidate inline
# ─────────────────────────────────────────────────────────────────────────────
def run_scrape(candidate_id: str) -> str | None:
    banner(f"4. scraper.scrape_candidate {candidate_id[:8]}")
    from services.scraper_svc.candidate import scrape_candidate, extract_candidate
    try:
        result = scrape_candidate.run(candidate_id)
        ok(f"scrape returned: {result}")
        gcs_ref = result.get("gcs_ref") if isinstance(result, dict) else None
        if not gcs_ref:
            warn("no gcs_ref — skipping extract.")
            return None
    except Exception as exc:
        fail(f"scrape raised: {type(exc).__name__}: {exc}")
        return None

    banner(f"4b. scraper.extract_candidate {candidate_id[:8]}")
    try:
        result = extract_candidate.run(candidate_id, gcs_ref)
        ok(f"extract returned: {result}")
        return result.get("scraped_product_id") if isinstance(result, dict) else None
    except Exception as exc:
        fail(f"extract raised: {type(exc).__name__}: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Verify
# ─────────────────────────────────────────────────────────────────────────────
def run_verify(candidate_id: str) -> bool:
    banner(f"5. scraper.verify_candidate {candidate_id[:8]}")
    from services.scraper_svc.candidate import verify_candidate
    try:
        result = verify_candidate.run(candidate_id)
        ok(f"verify returned: {result}")
        return isinstance(result, dict) and result.get("status") == "verified"
    except Exception as exc:
        fail(f"verify raised: {type(exc).__name__}: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 6. ProductLevelMatch + observations + stats + pricing
# ─────────────────────────────────────────────────────────────────────────────
def check_matches(product_id: str) -> int:
    banner("6. ProductLevelMatch rows")
    with get_db() as db:
        rows = db.execute(
            text("""
                SELECT id, "scrapedProductId", confidence, "confidenceTier", source
                FROM "ProductLevelMatch"
                WHERE "shopifyProductId" = :id
            """),
            {"id": product_id},
        ).all()
    for r in rows:
        print(f"   confidence={float(r.confidence):.3f} tier={r.confidenceTier} "
              f"source={r.source} scraped={r.scrapedProductId}")
    if not rows:
        warn("no ProductLevelMatch rows.")
    return len(rows)


def check_observations(product_id: str) -> int:
    banner("7. CompetitorPriceObservation rows (last 5)")
    with get_db() as db:
        rows = db.execute(
            text("""
                SELECT cpo."observedAt", cpo.price, sp.domain, sv.title
                FROM "CompetitorPriceObservation" cpo
                JOIN "ScrapedVariant" sv ON sv.id = cpo."competitorVariantId"
                JOIN "ScrapedProduct" sp ON sp.id = sv."productId"
                JOIN "ProductLevelMatch" plm ON plm."scrapedProductId" = sp.id
                WHERE plm."shopifyProductId" = :id
                ORDER BY cpo."observedAt" DESC
                LIMIT 5
            """),
            {"id": product_id},
        ).all()
    for r in rows:
        print(f"   {r.observedAt} {r.domain:30s} {r.title[:30]:30s} ${r.price}")
    if not rows:
        warn("no observations.")
    return len(rows)


def run_pricing(shop_domain: str, product_id: str) -> None:
    banner("8. pricing.decide_for_product (inline)")
    from services.pricing_svc.decide import decide_price_for_product
    try:
        result = decide_price_for_product(shop_domain, product_id)
        ok(f"decide returned: {result}")
    except Exception as exc:
        fail(f"decide raised: {type(exc).__name__}: {exc}")


def check_decisions(product_id: str) -> int:
    banner("9. PriceDecision rows (last 5 across variants)")
    with get_db() as db:
        rows = db.execute(
            text("""
                SELECT pd."decidedAt", pd."oldPrice", pd."newPrice",
                       pd.reason, pd."shopifyVariantId"
                FROM "PriceDecision" pd
                JOIN "ShopifyVariant" sv ON sv.id = pd."shopifyVariantId"
                WHERE sv."productId" = :id
                ORDER BY pd."decidedAt" DESC
                LIMIT 5
            """),
            {"id": product_id},
        ).all()
    for r in rows:
        print(f"   {r.decidedAt}  ${r.oldPrice} → ${r.newPrice}  ({r.reason})")
    if not rows:
        warn("no PriceDecisions.")
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    product_id_arg = sys.argv[1] if len(sys.argv) > 1 else None

    test_serper()
    test_vertex()
    test_groq_search_query()

    product = pick_product(product_id_arg)
    if not product:
        sys.exit(1)

    check_query_vector(product.id)

    n_candidates = run_discovery(product.id)
    candidate_ids = list_candidates(product.id)

    if not candidate_ids:
        warn("no usable candidates — stopping before scrape.")
        sys.exit(0)

    # Scrape + verify just the first candidate to keep the test fast.
    cid = candidate_ids[0]
    scraped_id = run_scrape(cid)
    if scraped_id:
        time.sleep(0.5)
        run_verify(cid)

    check_matches(product.id)
    check_observations(product.id)
    run_pricing(product.shopDomain, product.id)
    check_decisions(product.id)


if __name__ == "__main__":
    main()
