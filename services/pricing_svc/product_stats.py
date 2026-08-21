"""
services/pricing_svc/product_stats.py

Server-computed data for the Stats pages (product list + per-product
detail). Every derived field — the usable-competitor gate, a decision's
lifecycle status, clamp explanations, the competitor price series — is
computed here once so any client (browser UI, chatbot) sees the same
numbers without re-deriving decide.py's rules a second time.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.common.models import PriceDecision, ShopifyProduct, ShopifyVariant, ShopSettings

# Matches page only ever shows CONFIRMED/LIKELY, never WEAK — same tiers the
# gate above treats as usable, but review status isn't restricted to
# CONFIRMED here since PENDING LIKELY matches are exactly what need review.
_REVIEWABLE_TIER_SQL = """
    AND plm."reviewStatus" != 'REJECTED'
    AND plm."confidenceTier" IN ('CONFIRMED', 'LIKELY')
"""

DAYS = 30

# Same gate as decide.py's real pricing eligibility check: CONFIRMED counts
# unconditionally, LIKELY only once a merchant has explicitly confirmed it.
_USABLE_MATCH_SQL = """
    AND plm."reviewStatus" != 'REJECTED'
    AND (plm."confidenceTier" = 'CONFIRMED'
         OR (plm."confidenceTier" = 'LIKELY' AND plm."reviewStatus" = 'CONFIRMED'))
"""


def _decision_status(d) -> str:
    """d is a PriceDecision (ORM) or a raw-SQL row — both expose
    .appliedAt/.applyError/.autoApplied by attribute access."""
    if d.appliedAt:
        return "applied"
    if d.applyError:
        return "failed"
    if d.autoApplied:
        return "pending"
    return "skipped"


def _clamp_explanation(clamp_reason: str | None, reason: str, new_price: float) -> dict | None:
    if not clamp_reason:
        return None
    m = re.match(r"ref=([\d.]+)\s+target=([\d.]+)\s+tier=(\w+)\s+comps=(\d+)", reason)
    if not m:
        return None
    ref, target, tier, comps = m.groups()
    target_price = float(target)
    if clamp_reason == "clamped_per_round":
        return {
            "line1": f"Target ₹{target_price:.2f} → ₹{new_price:.2f} (per-round cap)",
            "line2": f"Limited by maximum change per cycle. Reference: ₹{ref} from {comps} competitors ({tier} tier).",
        }
    if clamp_reason == "clamped_lifetime_cap":
        return {
            "line1": f"Target ₹{target_price:.2f} → ₹{new_price:.2f} (lifetime cap)",
            "line2": f"Price adjusted to stay within allowed range. Reference: ₹{ref} from {comps} competitors ({tier} tier).",
        }
    return None


def get_product_stats(session: Session, shop_domain: str, product_id: str) -> dict:
    """Everything the per-product Stats page (or a chatbot) needs for one product."""
    product = session.get(ShopifyProduct, product_id)
    if product is None or product.shopDomain != shop_domain:
        raise ValueError(f"Product {product_id} not found in this shop.")

    variants = (
        session.query(ShopifyVariant)
        .filter(ShopifyVariant.productId == product_id)
        .order_by(ShopifyVariant.id.asc())
        .all()
    )
    variant_ids = [v.id for v in variants]

    decisions = (
        session.query(PriceDecision)
        .filter(PriceDecision.shopDomain == shop_domain, PriceDecision.shopifyVariantId.in_(variant_ids))
        .order_by(PriceDecision.decidedAt.desc())
        .limit(100)
        .all()
        if variant_ids else []
    )

    settings = session.get(ShopSettings, shop_domain)
    min_competitors = settings.minCompetitorsToPrice if settings else 4

    # Combines what used to be 3 separate round-trips (usable-match count,
    # top-8 matches for the chart, and a follow-up variant-ids-per-match
    # lookup) into one query: fetch every usable match already joined to its
    # competitor variants, then derive the count/top-8/variant-map in Python.
    # The gate count (`strong_match_count`) covers ALL usable matches, not
    # just the chart's top 8 — kept correct by counting distinct matches
    # from the full result before truncating to 8 for display.
    match_rows = session.execute(
        text(f"""
            SELECT plm.id AS "matchId", plm."scrapedProductId", plm.confidence,
                   sp.title, sp.domain, sv.id AS "competitorVariantId"
            FROM "ProductLevelMatch" plm
            JOIN "ScrapedProduct" sp ON sp.id = plm."scrapedProductId"
            LEFT JOIN "ScrapedVariant" sv ON sv."productId" = sp.id
            WHERE plm."shopifyProductId" = :pid AND plm."shopDomain" = :sd
            {_USABLE_MATCH_SQL}
            ORDER BY plm.confidence DESC, plm.id
        """),
        {"pid": product_id, "sd": shop_domain},
    ).all()

    match_by_scraped_product = {}
    variant_ids_by_scraped_product = {}
    for r in match_rows:
        if r.scrapedProductId not in match_by_scraped_product:
            match_by_scraped_product[r.scrapedProductId] = r
            variant_ids_by_scraped_product[r.scrapedProductId] = []
        if r.competitorVariantId is not None:
            variant_ids_by_scraped_product[r.scrapedProductId].append(r.competitorVariantId)

    strong_match_count = len(match_by_scraped_product)

    top8_scraped_ids = list(match_by_scraped_product.keys())[:8]
    variant_to_match = {
        vid: match_by_scraped_product[spid]
        for spid in top8_scraped_ids
        for vid in variant_ids_by_scraped_product[spid]
    }

    since = datetime.now(timezone.utc) - timedelta(days=DAYS)
    observations = (
        session.execute(
            text("""
                SELECT "competitorVariantId", price, "observedAt"
                FROM "CompetitorPriceObservation"
                WHERE "competitorVariantId" = ANY(:vids) AND "observedAt" >= :since
                ORDER BY "observedAt" ASC
            """),
            {"vids": list(variant_to_match.keys()), "since": since},
        ).all()
        if variant_to_match else []
    )

    series_by_competitor: dict[str, dict] = {}
    for o in observations:
        m = variant_to_match.get(o.competitorVariantId)
        if m is None:
            continue
        s = series_by_competitor.setdefault(m.scrapedProductId, {
            "id": m.scrapedProductId, "title": m.title, "domain": m.domain,
            "confidence": float(m.confidence), "points": [],
        })
        s["points"].append({"t": int(o.observedAt.timestamp() * 1000), "price": float(o.price)})

    numeric_product_id = product_id.split("/")[-1]
    store_handle = shop_domain.replace(".myshopify.com", "")
    admin_product_url = f"https://admin.shopify.com/store/{store_handle}/products/{numeric_product_id}"

    decisions_out = []
    for d in decisions:
        status = _decision_status(d)
        skip_reason = d.skipReason
        clamp_reason = skip_reason if skip_reason and skip_reason.startswith("clamped_") else None
        skip_reason_not_clamp = skip_reason if skip_reason and not skip_reason.startswith("clamped_") else None
        decisions_out.append({
            "id": d.id,
            "variantId": d.shopifyVariantId,
            "oldPrice": float(d.oldPrice),
            "newPrice": float(d.newPrice),
            "changePct": d.changePct,
            "refPrice": float(d.refPrice) if d.refPrice is not None else None,
            "tier": d.tierAtDecision,
            "competitorsUsed": d.competitorsUsed,
            "oosObservations": d.oosObservations,
            "currencyDrops": d.currencyDrops,
            "appliedAt": d.appliedAt.isoformat() if d.appliedAt else None,
            "revertedAt": d.revertedAt.isoformat() if d.revertedAt else None,
            "decidedAt": d.decidedAt.isoformat(),
            "reason": d.reason,
            "applyError": d.applyError,
            "status": status,
            "clampReason": clamp_reason,
            "skipReason": skip_reason_not_clamp,
            "clampExplanation": _clamp_explanation(clamp_reason, d.reason, float(d.newPrice)),
        })

    return {
        "waiting": {"have": strong_match_count, "need": min_competitors},
        "product": {
            "id": product.id,
            "title": product.title,
            "dynamicPricingEnabled": product.dynamicPricingEnabled,
            "tier": product.pricingTier,
            "avgBasePrice": float(product.avgBasePrice) if product.avgBasePrice is not None else None,
            "adminProductUrl": admin_product_url,
            "variants": [
                {
                    "id": v.id, "title": v.title,
                    "currentPrice": float(v.currentPrice),
                    "basePrice": float(v.basePrice) if v.basePrice is not None else None,
                }
                for v in variants
            ],
        },
        "decisions": decisions_out,
        "competitorSeries": list(series_by_competitor.values()),
    }


def list_product_stats(session: Session, shop_domain: str) -> list[dict]:
    """Everything the Stats product-list page (or a chatbot) needs."""
    products = (
        session.query(ShopifyProduct)
        .filter(
            ShopifyProduct.shopDomain == shop_domain,
            (ShopifyProduct.dynamicPricingEnabled.is_(True)) | (ShopifyProduct.lastDecisionAt.isnot(None)),
        )
        .order_by(ShopifyProduct.lastDecisionAt.desc().nullslast())
        .all()
    )
    product_ids = [p.id for p in products]
    if not product_ids:
        return []

    variant_rows = session.execute(
        text("""
            SELECT DISTINCT ON ("productId") "productId", "currentPrice"
            FROM "ShopifyVariant"
            WHERE "productId" = ANY(:pids)
            ORDER BY "productId", id ASC
        """),
        {"pids": product_ids},
    ).all()
    price_by_product = {r.productId: float(r.currentPrice) for r in variant_rows}

    recent = session.execute(
        text("""
            SELECT DISTINCT ON (v."productId")
                   v."productId" AS pid, pd."changePct", pd."autoApplied",
                   pd."appliedAt", pd."applyError", pd."skipReason", pd."decidedAt"
            FROM "PriceDecision" pd
            JOIN "ShopifyVariant" v ON v.id = pd."shopifyVariantId"
            WHERE v."productId" = ANY(:pids)
            ORDER BY v."productId", pd."decidedAt" DESC
        """),
        {"pids": product_ids},
    ).all()
    recent_by_pid = {r.pid: r for r in recent}

    out = []
    for p in products:
        r = recent_by_pid.get(p.id)
        status = _decision_status(r) if r else "none"
        out.append({
            "id": p.id,
            "title": p.title,
            "imageUrl": p.imageUrl,
            "tier": p.pricingTier,
            "currentPrice": price_by_product.get(p.id),
            "avgBasePrice": float(p.avgBasePrice) if p.avgBasePrice is not None else None,
            "lastDecisionAt": r.decidedAt.isoformat() if r and r.decidedAt else None,
            "lastChangePct": r.changePct if r else None,
            "lastStatus": status,
            "lastSkipReason": r.skipReason if r else None,
        })
    return out


def get_match_activity(session: Session, shop_domain: str, product_id: str, since: datetime) -> list[dict]:
    """Match lifecycle events (discovered / confirmed / rejected) for the History
    page's activity feed. Ported from shopify_ui/app/routes/app.history.$id.jsx,
    which independently synthesized this from raw ProductLevelMatch rows — moved
    here so any client sees the same events, and so rejected matches (previously
    dropped by a query filter that made the JS "rejected" branch unreachable)
    actually show up."""
    rows = session.execute(
        text("""
            SELECT plm.id, plm."createdAt", plm."reviewStatus", plm."reviewedAt",
                   sp.title, sp.domain
            FROM "ProductLevelMatch" plm
            JOIN "ScrapedProduct" sp ON sp.id = plm."scrapedProductId"
            WHERE plm."shopifyProductId" = :pid AND plm."shopDomain" = :sd
              AND (plm."createdAt" >= :since OR plm."reviewedAt" >= :since)
            ORDER BY plm."createdAt" DESC
        """),
        {"pid": product_id, "sd": shop_domain, "since": since},
    ).all()

    events = []
    for m in rows:
        if m.createdAt and m.createdAt >= since:
            events.append({
                "matchId": m.id, "type": "created", "timestamp": m.createdAt.isoformat(),
                "description": f"New competitor discovered: {m.title} ({m.domain})",
            })
        if m.reviewStatus == "CONFIRMED" and m.reviewedAt and m.reviewedAt >= since:
            events.append({
                "matchId": m.id, "type": "confirmed", "timestamp": m.reviewedAt.isoformat(),
                "description": f"Confirmed match: {m.title}",
            })
        if m.reviewStatus == "REJECTED" and m.reviewedAt and m.reviewedAt >= since:
            events.append({
                "matchId": m.id, "type": "rejected", "timestamp": m.reviewedAt.isoformat(),
                "description": f"Rejected match: {m.title}",
            })

    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events


def _latest_competitor_prices(session: Session, scraped_product_ids: list[str]) -> dict[str, float]:
    if not scraped_product_ids:
        return {}
    rows = session.execute(
        text("""
            SELECT DISTINCT ON ("productId") "productId", "currentPrice"
            FROM "ScrapedVariant"
            WHERE "productId" = ANY(:pids)
            ORDER BY "productId", "updatedAt" DESC
        """),
        {"pids": scraped_product_ids},
    ).all()
    return {r.productId: float(r.currentPrice) for r in rows}


def _competitor_urls(session: Session, scraped_product_ids: list[str]) -> dict[str, str]:
    if not scraped_product_ids:
        return {}
    rows = session.execute(
        text('SELECT "prodId", url FROM "ProductUrl" WHERE "prodId" = ANY(:pids)'),
        {"pids": scraped_product_ids},
    ).all()
    return {r.prodId: r.url for r in rows}


def list_matches_for_review(session: Session, shop_domain: str) -> dict:
    """Grouped-by-product view for the Matches page (or a chatbot): one top
    match per product plus counts, ported from app.matches.jsx's loader,
    which built this shape itself in JS with its own Prisma queries."""
    rows = session.execute(
        text(f"""
            SELECT plm.id, plm.confidence, plm."confidenceTier", plm.source,
                   plm."reviewStatus", plm."reviewedAt", plm."shopifyProductId",
                   plm."scrapedProductId",
                   sp.title AS "scrapedTitle", sp.domain AS "scrapedDomain",
                   sp."imageUrl" AS "scrapedImageUrl"
            FROM "ProductLevelMatch" plm
            JOIN "ScrapedProduct" sp ON sp.id = plm."scrapedProductId"
            WHERE plm."shopDomain" = :sd
            {_REVIEWABLE_TIER_SQL}
            ORDER BY plm."shopifyProductId" ASC, plm.confidence DESC
        """),
        {"sd": shop_domain},
    ).all()

    if not rows:
        return {
            "metrics": {"totalProducts": 0, "pendingReviews": 0, "reviewPercentage": 0, "avgConfidence": 0.0},
            "groups": [],
        }

    pending_reviews = sum(1 for r in rows if r.reviewedAt is None)
    total_matches = len(rows)
    reviewed_matches = total_matches - pending_reviews
    review_percentage = round((reviewed_matches / total_matches) * 100)
    avg_confidence = round(sum(float(r.confidence) for r in rows) / total_matches, 1)

    product_ids = list({r.shopifyProductId for r in rows})
    product_rows = session.execute(
        text("""
            SELECT p.id, p.title, p."imageUrl",
                   (SELECT v."currentPrice" FROM "ShopifyVariant" v
                    WHERE v."productId" = p.id ORDER BY v.id ASC LIMIT 1) AS "merchantPrice"
            FROM "ShopifyProduct" p
            WHERE p.id = ANY(:pids)
        """),
        {"pids": product_ids},
    ).all()
    product_by_id = {p.id: p for p in product_rows}

    scraped_ids = list({r.scrapedProductId for r in rows})
    url_by_scraped = _competitor_urls(session, scraped_ids)
    price_by_scraped = _latest_competitor_prices(session, scraped_ids)

    groups: dict[str, dict] = {}
    for r in rows:
        p = product_by_id.get(r.shopifyProductId)
        if p is None:
            continue
        grp = groups.setdefault(r.shopifyProductId, {
            "id": p.id, "title": p.title, "imageUrl": p.imageUrl,
            "merchantPrice": str(p.merchantPrice) if p.merchantPrice is not None else None,
            "matchCount": 0, "topMatch": None,
        })
        grp["matchCount"] += 1
        if grp["topMatch"] is None:
            grp["topMatch"] = {
                "id": r.id,
                "confidence": float(r.confidence),
                "confidenceTier": r.confidenceTier,
                "source": r.source,
                "reviewStatus": r.reviewStatus,
                "scrapedTitle": r.scrapedTitle,
                "scrapedDomain": r.scrapedDomain,
                "scrapedImageUrl": r.scrapedImageUrl,
                "competitorPrice": str(price_by_scraped[r.scrapedProductId]) if r.scrapedProductId in price_by_scraped else None,
                "competitorUrl": url_by_scraped.get(r.scrapedProductId),
                "scrapedProductId": r.scrapedProductId,
            }

    return {
        "metrics": {
            "totalProducts": len(groups),
            "pendingReviews": pending_reviews,
            "reviewPercentage": review_percentage,
            "avgConfidence": avg_confidence,
        },
        "groups": list(groups.values()),
    }


def list_matches_for_product(session: Session, shop_domain: str, product_id: str, limit: int) -> dict:
    """One product's reviewable matches (top `limit` by confidence), ported
    from app.matches.lazy.jsx's loader."""
    total_count = session.execute(
        text(f"""
            SELECT COUNT(*) FROM "ProductLevelMatch" plm
            WHERE plm."shopDomain" = :sd AND plm."shopifyProductId" = :pid
            {_REVIEWABLE_TIER_SQL}
        """),
        {"sd": shop_domain, "pid": product_id},
    ).scalar()

    rows = session.execute(
        text(f"""
            SELECT plm.id, plm.confidence, plm."confidenceTier", plm.source, plm."reviewStatus",
                   plm."scrapedProductId",
                   sp.title AS "scrapedTitle", sp.domain AS "scrapedDomain", sp."imageUrl" AS "scrapedImageUrl"
            FROM "ProductLevelMatch" plm
            JOIN "ScrapedProduct" sp ON sp.id = plm."scrapedProductId"
            WHERE plm."shopDomain" = :sd AND plm."shopifyProductId" = :pid
            {_REVIEWABLE_TIER_SQL}
            ORDER BY plm.confidence DESC
            LIMIT :limit
        """),
        {"sd": shop_domain, "pid": product_id, "limit": limit},
    ).all()

    scraped_ids = [r.scrapedProductId for r in rows]
    url_by_scraped = _competitor_urls(session, scraped_ids)
    price_by_scraped = _latest_competitor_prices(session, scraped_ids)

    matches = [
        {
            "id": r.id,
            "confidence": float(r.confidence),
            "confidenceTier": r.confidenceTier,
            "source": r.source,
            "reviewStatus": r.reviewStatus,
            "scrapedTitle": r.scrapedTitle,
            "scrapedDomain": r.scrapedDomain,
            "scrapedImageUrl": r.scrapedImageUrl,
            "competitorPrice": str(price_by_scraped[r.scrapedProductId]) if r.scrapedProductId in price_by_scraped else None,
            "competitorUrl": url_by_scraped.get(r.scrapedProductId),
            "scrapedProductId": r.scrapedProductId,
        }
        for r in rows
    ]

    return {"matches": matches, "totalCount": total_count}
