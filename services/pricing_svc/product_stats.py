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

    strong_match_count = session.execute(
        text(f"""
            SELECT COUNT(*) FROM "ProductLevelMatch" plm
            WHERE plm."shopifyProductId" = :pid AND plm."shopDomain" = :sd
            {_USABLE_MATCH_SQL}
        """),
        {"pid": product_id, "sd": shop_domain},
    ).scalar()

    matches = session.execute(
        text(f"""
            SELECT plm.id, plm."scrapedProductId", plm.confidence,
                   sp.title, sp.domain
            FROM "ProductLevelMatch" plm
            JOIN "ScrapedProduct" sp ON sp.id = plm."scrapedProductId"
            WHERE plm."shopifyProductId" = :pid AND plm."shopDomain" = :sd
            {_USABLE_MATCH_SQL}
            ORDER BY plm.confidence DESC
            LIMIT 8
        """),
        {"pid": product_id, "sd": shop_domain},
    ).all()
    match_by_scraped_product = {m.scrapedProductId: m for m in matches}

    scraped_product_ids = list(match_by_scraped_product.keys())
    competitor_variant_rows = (
        session.execute(
            text('SELECT id, "productId" FROM "ScrapedVariant" WHERE "productId" = ANY(:pids)'),
            {"pids": scraped_product_ids},
        ).all()
        if scraped_product_ids else []
    )
    variant_to_match = {r.id: match_by_scraped_product[r.productId] for r in competitor_variant_rows}

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
        .filter(ShopifyProduct.shopDomain == shop_domain, ShopifyProduct.lastDecisionAt.isnot(None))
        .order_by(ShopifyProduct.lastDecisionAt.desc())
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
