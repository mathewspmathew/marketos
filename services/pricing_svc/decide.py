"""
services/pricing_svc/decide.py

v1 product-level pricing decision.

Formula (deliberately dumb):
  suggested = median(latest competitor prices across the product's matches)
            × (1 - shop.markupPct)
  clamped to (product.floorPrice, product.ceilingPrice) when set.

The same price is written to every variant of the product (the UX is product-
level for v1). One PriceDecision row per variant is created so the audit
trail and the price-history dashboard see consistent per-variant timelines.

A decision is NOT generated when:
  - dynamicPricingEnabled is false
  - killSwitch is true
  - the product has fewer than ShopSettings.minCompetitorsRequired observations
  - the resulting price equals (within 0.5%) the current price on the variant
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text

from services.common.db import get_db

logger = logging.getLogger(__name__)

MIN_CHANGE_PCT = Decimal("0.005")  # ignore sub-0.5% wiggles


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _percentile(sorted_vals: list[Decimal], pct: float) -> Decimal | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = Decimal(str(k - lo))
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def decide_price_for_product(shop_domain: str, shopify_product_id: str) -> dict:
    """Run the v1 formula and write PriceDecisions. Returns a small status dict."""
    with get_db() as session:
        product_row = session.execute(
            text("""
                SELECT id, "dynamicPricingEnabled", "floorPrice", "ceilingPrice"
                FROM "ShopifyProduct"
                WHERE id = :pid AND "shopDomain" = :sd
            """),
            {"pid": shopify_product_id, "sd": shop_domain},
        ).first()
        if not product_row:
            return {"ok": False, "reason": "product_missing"}
        if not product_row.dynamicPricingEnabled:
            return {"ok": False, "reason": "pricing_disabled"}

        settings = session.execute(
            text("""
                SELECT "markupPct", "minCompetitorsRequired", "killSwitch"
                FROM "ShopSettings" WHERE "shopDomain" = :sd
            """),
            {"sd": shop_domain},
        ).first()
        markup    = Decimal(str(settings.markupPct))             if settings else Decimal("0.02")
        min_comps = settings.minCompetitorsRequired              if settings else 2
        if settings and settings.killSwitch:
            return {"ok": False, "reason": "kill_switch"}

        # Latest observation per competitor variant matched to this product
        # via ProductLevelMatch → ScrapedProduct → ScrapedVariant.
        obs_rows = session.execute(
            text("""
                SELECT DISTINCT ON (sv.id)
                       sv.id              AS scraped_variant_id,
                       cpo.price          AS price,
                       cpo."isInStock"    AS in_stock,
                       comp.enabled       AS comp_enabled
                FROM "ProductLevelMatch" plm
                JOIN "ScrapedProduct"  sp ON sp.id  = plm."scrapedProductId"
                JOIN "ScrapedVariant"  sv ON sv."productId" = sp.id
                LEFT JOIN "Competitor" comp
                       ON comp."shopDomain" = plm."shopDomain"
                      AND comp.domain       = sp.domain
                LEFT JOIN "CompetitorPriceObservation" cpo
                       ON cpo."competitorVariantId" = sv.id
                WHERE plm."shopifyProductId" = :pid
                  AND plm."shopDomain"       = :sd
                  AND plm."rejectedByMerchant" = FALSE
                ORDER BY sv.id, cpo."observedAt" DESC NULLS LAST
            """),
            {"pid": shopify_product_id, "sd": shop_domain},
        ).all()

        prices = [
            Decimal(str(r.price)) for r in obs_rows
            if r.price is not None
            and r.in_stock is not False
            and (r.comp_enabled is None or r.comp_enabled is True)
            and Decimal(str(r.price)) > 0
        ]

        if len(prices) < min_comps:
            return {"ok": False, "reason": "not_enough_competitors", "have": len(prices), "need": min_comps}

        prices.sort()
        median   = _percentile(prices, 0.5)
        proposed = _q(median * (Decimal("1") - markup))

        floor   = Decimal(str(product_row.floorPrice))   if product_row.floorPrice   is not None else None
        ceiling = Decimal(str(product_row.ceilingPrice)) if product_row.ceilingPrice is not None else None
        if floor   is not None and proposed < floor:
            proposed = floor
        if ceiling is not None and proposed > ceiling:
            proposed = ceiling

        variants = session.execute(
            text('SELECT id, "currentPrice" FROM "ShopifyVariant" WHERE "productId" = :pid'),
            {"pid": shopify_product_id},
        ).all()
        if not variants:
            return {"ok": False, "reason": "no_variants"}

        written = 0
        skipped = 0
        reason  = f"v1 median × (1 - {markup}) over {len(prices)} competitor observation(s)"
        now     = datetime.now(timezone.utc)

        for v in variants:
            cur = Decimal(str(v.currentPrice))
            if cur > 0 and abs(proposed - cur) / cur < MIN_CHANGE_PCT:
                skipped += 1
                continue
            session.execute(
                text("""
                    INSERT INTO "PriceDecision"
                        (id, "shopDomain", "shopifyVariantId",
                         "oldPrice", "newPrice", reason, "decidedAt")
                    VALUES (gen_random_uuid(), :sd, :vid, :old, :new, :reason, :now)
                """),
                {"sd": shop_domain, "vid": v.id, "old": cur, "new": proposed, "reason": reason, "now": now},
            )
            written += 1

    return {
        "ok": True,
        "written": written,
        "skipped_unchanged": skipped,
        "median": str(median),
        "new_price": str(proposed),
        "competitors": len(prices),
    }
