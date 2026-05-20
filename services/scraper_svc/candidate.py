"""
services/scraper_svc/candidate.py

Candidate-driven scrape pipeline (kicked off by discovery_svc).

Tasks:
  scraper.scrape_candidate(candidate_id)
    Firecrawl a single product URL → upload markdown to GCS → enqueue extract.

  scraper.extract_candidate(candidate_id, gcs_ref)
    Groq extract → upsert ScrapedProduct + ScrapedVariants → create a
    product-rooted ProductUrl    row (with shopifyProductId + frequency) →
    fan out to embedding worker and verify_candidate.

  scraper.verify_candidate(candidate_id)
    Embed the scraped product's title → cosine vs ShopifyProduct.searchQueryVector.
    Above VERIFY_THRESHOLD: candidate VERIFIED + create ProductLevelMatch.
    Below: candidate REJECTED with reason.

This file does NOT touch the legacy listing-based scrape (scraper.scrape_listing).
That code path stays callable until the scheduler rewrite drops it.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlparse

from dotenv import load_dotenv
from firecrawl import V1FirecrawlApp
from groq import RateLimitError as GroqRateLimitError
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from services.common.celery_app import app
from services.common.db import get_db
from services.common.gcs_utils import (
    download_markdown_from_gcs,
    upload_image_to_gcs,
    upload_markdown_to_gcs,
)
from services.common import models
from services.common.frequency import next_run_at as _freq_next_run_at
from services.common.vertex_embed import (
    cosine,
    embed_text,
    load_search_query_vector,
)
from services.scraper_svc.extractor import (
    _record_observations,
    extract_with_groq,
    update_prices_in_db,
)
from services.scraper_svc.helpers import log_error

load_dotenv()

logger = logging.getLogger(__name__)

_firecrawl_client = V1FirecrawlApp(api_key=os.getenv("FIRECRAWL_API_KEY", "not-set"))

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────
VERIFY_THRESHOLD       = 0.78   # cosine on title vs searchQueryVector to keep
MIN_MARKDOWN_LEN       = 400
FIRECRAWL_TIMEOUT_MS   = 45000


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _set_candidate_status(
    db, candidate_id: str, *, status: str | None = None, **fields
) -> None:
    """Convenience setter for CompetitorCandidate fields."""
    values: dict = dict(fields)
    if status is not None:
        values["status"] = status
    if not values:
        return
    db.execute(
        sa_update(models.CompetitorCandidate)
        .where(models.CompetitorCandidate.id == candidate_id)
        .values(**values)
    )


def _next_run_at(product: models.ShopifyProduct, settings: models.ShopSettings | None) -> datetime:
    """Resolve rescrape cadence: per-product override → shop default → daily."""
    unit     = product.frequencyUnit     or (settings.frequencyUnit     if settings else None)
    interval = product.frequencyInterval or (settings.frequencyInterval if settings else None)
    return _freq_next_run_at(interval, unit)


# ─────────────────────────────────────────────────────────────────────────────
# Task 1 — scrape_candidate
# ─────────────────────────────────────────────────────────────────────────────
@app.task(
    name="scraper.scrape_candidate",
    bind=True, max_retries=3, default_retry_delay=60,
    time_limit=180, soft_time_limit=150,
)
def scrape_candidate(self, candidate_id: str):
    """Firecrawl a single candidate URL → GCS → enqueue extract."""
    with get_db() as db:
        cand = db.get(models.CompetitorCandidate, candidate_id)
        if not cand:
            logger.warning("scrape_candidate: candidate %s missing", candidate_id)
            return {"status": "missing"}
        url    = cand.url
        domain = cand.domain

    try:
        result   = _firecrawl_client.scrape_url(url, formats=["markdown"], timeout=FIRECRAWL_TIMEOUT_MS)
        markdown = (result.get("markdown") if isinstance(result, dict)
                    else getattr(result, "markdown", None)) or ""
    except Exception as exc:
        logger.warning("Firecrawl failed for %s: %s", url, exc)
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(
                    db, candidate_id,
                    status="DEAD", rejectReason=f"firecrawl_failed: {type(exc).__name__}",
                )
            return {"status": "dead"}
        raise self.retry(exc=exc)

    if len(markdown.strip()) < MIN_MARKDOWN_LEN:
        with get_db() as db:
            _set_candidate_status(
                db, candidate_id,
                status="REJECTED", rejectReason="markdown_too_short",
            )
        return {"status": "rejected_short_md"}

    gcs_ref = upload_markdown_to_gcs(markdown, domain, url)
    if not gcs_ref:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(
                    db, candidate_id,
                    status="DEAD", rejectReason="gcs_upload_failed",
                )
            return {"status": "dead"}
        raise self.retry(exc=RuntimeError("GCS upload failed"))

    app.send_task(
        "scraper.extract_candidate",
        args=[candidate_id, gcs_ref],
        queue="extraction_queue",
    )
    return {"status": "queued_extract", "gcs_ref": gcs_ref}


# ─────────────────────────────────────────────────────────────────────────────
# Task 2 — extract_candidate
# ─────────────────────────────────────────────────────────────────────────────
@app.task(
    name="scraper.extract_candidate",
    bind=True, max_retries=5, default_retry_delay=30, rate_limit="3/m",
)
def extract_candidate(self, candidate_id: str, gcs_ref: str):
    """Groq extract → ScrapedProduct + product-rooted ProductUrl → fan out."""
    with get_db() as db:
        cand = db.get(models.CompetitorCandidate, candidate_id)
        if not cand:
            return {"status": "missing"}
        shop_domain        = cand.shopDomain
        url                = cand.url
        domain             = cand.domain
        shopify_product_id = cand.shopifyProductId

    markdown = download_markdown_from_gcs(gcs_ref)
    if not markdown:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="DEAD", rejectReason="gcs_empty")
            log_error(shop_domain, None, url, "GCS_EMPTY", "scraper.extract_candidate", gcs_ref, "")
            return {"status": "dead"}
        raise self.retry(exc=ValueError("empty markdown"))

    try:
        product = extract_with_groq(markdown, url)
    except GroqRateLimitError:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="DEAD", rejectReason="groq_rate_limited")
            return {"status": "dead"}
        raise self.retry(countdown=65)

    if not product or not product.title:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="REJECTED", rejectReason="groq_no_product")
            return {"status": "rejected_no_product"}
        raise self.retry(exc=ValueError("groq returned nothing"))

    image_url = ""
    if product.image_url and product.image_url.startswith("http"):
        image_url = upload_image_to_gcs(product.image_url) or ""

    # ── DB write ───────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    prod_id: str | None = None
    try:
        with get_db() as db:
            shopify_product = db.get(models.ShopifyProduct, shopify_product_id)
            settings        = db.get(models.ShopSettings, shop_domain)

            existing = (
                db.query(models.ProductUrl)
                .filter(models.ProductUrl.url == url)
                .first()
            )
            if existing:
                prod_id = existing.prodId
                db.execute(
                    sa_update(models.ScrapedProduct)
                    .where(models.ScrapedProduct.id == prod_id)
                    .values(
                        title=product.title,
                        description=product.description or "",
                        vendor=product.vendor or "",
                        productType=product.product_type or "",
                        tags=product.tags or [],
                        imageUrl=image_url,
                        specifications=(
                            json.loads(json.dumps(product.specifications))
                            if product.specifications else None
                        ),
                        updatedAt=now,
                    )
                )
            else:
                prod_id = str(uuid.uuid4())
                db.execute(
                    pg_insert(models.ScrapedProduct).values(
                        id=prod_id,
                        shopDomain=shop_domain,
                        domain=domain,
                        title=product.title,
                        description=product.description or "",
                        vendor=product.vendor or "",
                        productType=product.product_type or "",
                        tags=product.tags or [],
                        imageUrl=image_url,
                        specifications=(
                            json.loads(json.dumps(product.specifications))
                            if product.specifications else None
                        ),
                        updatedAt=now,
                    )
                )

            # Product-rooted URL row. configId is null (new flow).
            next_run = _next_run_at(shopify_product, settings) if shopify_product else None
            db.execute(
                pg_insert(models.ProductUrl)
                .values(
                    id=str(uuid.uuid4()),
                    shopDomain=shop_domain,
                    shopifyProductId=shopify_product_id,
                    configId=None,
                    prodId=prod_id,
                    url=url,
                    status="ACTIVE",
                    failCount=0,
                    frequencyInterval=(shopify_product.frequencyInterval if shopify_product else None),
                    frequencyUnit=(shopify_product.frequencyUnit if shopify_product else None),
                    lastScrapedAt=now,
                    nextRunAt=next_run,
                )
                .on_conflict_do_update(
                    index_elements=["url"],
                    set_={
                        "shopifyProductId": shopify_product_id,
                        "prodId":           prod_id,
                        "status":           "ACTIVE",
                        "failCount":        0,
                        "lastScrapedAt":    now,
                        "nextRunAt":        next_run,
                    },
                )
            )

            # Replace variants for this product.
            db.query(models.ScrapedVariant).filter(
                models.ScrapedVariant.productId == prod_id
            ).delete(synchronize_session=False)

            variants = product.variants or []
            if len(variants) == 1:
                variants[0].title = product.title

            variant_rows = [
                {
                    "id":            str(uuid.uuid4()),
                    "productId":     prod_id,
                    "sku":           str(v.sku or ""),
                    "barcode":       v.barcode,
                    "title":         v.title,
                    "options":       v.options,
                    "currentPrice":  float(v.current_price or 0),
                    "originalPrice": float(v.original_price) if v.original_price else None,
                    "isInStock":     bool(v.is_in_stock),
                    "stockQuantity": v.stock_quantity,
                    "updatedAt":     now,
                }
                for v in variants
            ]
            if variant_rows:
                db.execute(pg_insert(models.ScrapedVariant), variant_rows)

                _record_observations(
                    db, shop_domain,
                    [
                        {
                            "competitorVariantId": r["id"],
                            "price":               r["currentPrice"],
                            "isInStock":           r["isInStock"],
                        }
                        for r in variant_rows
                        if r["currentPrice"] and r["currentPrice"] > 0
                    ],
                )

            _set_candidate_status(
                db, candidate_id,
                scrapedProductId=prod_id, scrapedAt=now,
            )
    except Exception as exc:
        logger.exception("extract_candidate DB error for %s", url)
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="DEAD", rejectReason=f"db_error: {type(exc).__name__}")
            return {"status": "dead"}
        raise self.retry(exc=exc)

    # ── Fan out: variant semantics → embedding → verify ────────────────────
    # Semantics → embeddings → matcher are the same chain the legacy listing
    # scrape uses. Verify runs in parallel since it only needs the merchant's
    # searchQueryVector + the scraped product's title (independent of variants).
    app.send_task(
        "scraper.generate_variant_semantics",
        args=[prod_id, None, shop_domain, url],
        queue="semantic_queue",
    )
    app.send_task("embedder.generate_embeddings", args=[prod_id], queue="embedding_queue")
    app.send_task("scraper.verify_candidate",    args=[candidate_id], queue="extraction_queue")
    return {"status": "queued_verify", "scraped_product_id": prod_id}


# ─────────────────────────────────────────────────────────────────────────────
# Task 3 — verify_candidate
# ─────────────────────────────────────────────────────────────────────────────
@app.task(
    name="scraper.verify_candidate",
    bind=True, max_retries=2, default_retry_delay=15,
)
def verify_candidate(self, candidate_id: str):
    """Embed scraped title, cosine vs cached searchQueryVector, decide.

    Uses the same encoder + task type the search-query vector was generated
    with, so the comparison is symmetric. The full multimodal embedding done
    by embedder.generate_embeddings is used downstream by the matcher for
    variant-level price stats — not by this gate.
    """
    with get_db() as db:
        cand = db.get(models.CompetitorCandidate, candidate_id)
        if not cand or not cand.scrapedProductId:
            return {"status": "missing_scrape"}
        shopify_product_id = cand.shopifyProductId
        scraped = db.get(models.ScrapedProduct, cand.scrapedProductId)
        if not scraped:
            _set_candidate_status(db, candidate_id, status="REJECTED", rejectReason="scrape_missing")
            return {"status": "rejected"}

        product_vec = load_search_query_vector(db, shopify_product_id)
        if not product_vec:
            # No cached vector → cannot verify deterministically; leave PENDING
            # for a later re-run rather than rejecting a possibly-good match.
            return {"status": "pending_no_vec"}

        # Build a short keyword-shape candidate side so the encoders match
        # what searchQueryVector saw (short title + vendor + category).
        cand_text = " | ".join(
            p for p in (
                (scraped.title or "").strip(),
                (scraped.vendor or "").strip(),
                (scraped.productType or "").strip(),
            ) if p
        )[:1500]
        cand_vec = embed_text(cand_text, task_type="RETRIEVAL_DOCUMENT") or []
        score = cosine(product_vec, cand_vec)

        now = datetime.now(timezone.utc)
        if score >= VERIFY_THRESHOLD:
            _set_candidate_status(
                db, candidate_id,
                status="VERIFIED", verifiedAt=now,
                rerankReason=f"verify_cosine={score:.4f}",
            )
            # Upsert ProductLevelMatch — the matched-products page reads this.
            db.execute(
                pg_insert(models.ProductLevelMatch.__table__)
                .values(
                    id=str(uuid.uuid4()),
                    shopDomain=cand.shopDomain,
                    shopifyProductId=shopify_product_id,
                    scrapedProductId=scraped.id,
                    confidence=Decimal(f"{min(0.999, score):.3f}"),
                    confidenceTier=("CONFIRMED" if score >= 0.88 else "LIKELY"),
                    source="DISCOVERY",
                    createdAt=now,
                    updatedAt=now,
                )
                .on_conflict_do_update(
                    index_elements=["shopifyProductId", "scrapedProductId"],
                    set_={
                        "confidence":     Decimal(f"{min(0.999, score):.3f}"),
                        "confidenceTier": ("CONFIRMED" if score >= 0.88 else "LIKELY"),
                        "updatedAt":      now,
                    },
                )
            )
            return {"status": "verified", "score": score}

        _set_candidate_status(
            db, candidate_id,
            status="REJECTED",
            rejectReason=f"verify_cosine={score:.4f}_below_{VERIFY_THRESHOLD}",
        )
        return {"status": "rejected", "score": score}


# ─────────────────────────────────────────────────────────────────────────────
# Task 4 — rescrape_url (called by the scheduler tick)
# ─────────────────────────────────────────────────────────────────────────────
@app.task(
    name="scraper.rescrape_url",
    bind=True, max_retries=3, default_retry_delay=60,
    time_limit=180, soft_time_limit=150,
)
def rescrape_url(self, product_url_id: str):
    """Periodic refresh for a product-rooted ProductUrl.

    Light-weight relative to scrape_candidate: we only update variant prices/
    stock and record a new CompetitorPriceObservation. No re-verification, no
    new ProductLevelMatch — that gate was passed when the URL was created.
    """
    with get_db() as db:
        pu = db.get(models.ProductUrl, product_url_id)
        if not pu or pu.status != "ACTIVE":
            return {"status": "skipped"}
        url         = pu.url
        domain      = pu.domain if hasattr(pu, "domain") else urlparse(url).netloc
        prod_id     = pu.prodId
        shop_domain = pu.shopDomain
        shopify_product_id = pu.shopifyProductId
        # Pre-fetch the cadence inputs so we can advance nextRunAt later.
        shopify_product = (
            db.get(models.ShopifyProduct, shopify_product_id)
            if shopify_product_id else None
        )
        settings = db.get(models.ShopSettings, shop_domain)

    # ── Firecrawl ──────────────────────────────────────────────────────────
    try:
        result   = _firecrawl_client.scrape_url(url, formats=["markdown"], timeout=FIRECRAWL_TIMEOUT_MS)
        markdown = (result.get("markdown") if isinstance(result, dict)
                    else getattr(result, "markdown", None)) or ""
    except Exception as exc:
        logger.warning("rescrape Firecrawl failed for %s: %s", url, exc)
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                db.execute(
                    sa_update(models.ProductUrl)
                    .where(models.ProductUrl.id == product_url_id)
                    .values(failCount=models.ProductUrl.failCount + 1)
                )
            return {"status": "fail"}
        raise self.retry(exc=exc)

    if len(markdown.strip()) < MIN_MARKDOWN_LEN:
        with get_db() as db:
            db.execute(
                sa_update(models.ProductUrl)
                .where(models.ProductUrl.id == product_url_id)
                .values(failCount=models.ProductUrl.failCount + 1)
            )
        return {"status": "fail_short_md"}

    # Reuse the rescrape extraction path: Groq → variant price update.
    try:
        product = extract_with_groq(markdown, url)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            return {"status": "fail_extract"}
        raise self.retry(exc=exc)

    if not product or not product.title:
        return {"status": "no_product"}

    ok = update_prices_in_db(prod_id, url, product)
    if not ok:
        return {"status": "no_update"}

    # Advance the schedule and clear failCount on success.
    next_at = _next_run_at(shopify_product, settings) if shopify_product else None
    with get_db() as db:
        db.execute(
            sa_update(models.ProductUrl)
            .where(models.ProductUrl.id == product_url_id)
            .values(
                lastScrapedAt=datetime.now(timezone.utc),
                nextRunAt=next_at,
                failCount=0,
            )
        )
    return {"status": "rescraped", "next_run_at": next_at.isoformat() if next_at else None}