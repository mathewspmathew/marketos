"""
services/pricing_svc/stats.py

v1 competitor-stats refresh.

For each merchant variant matched to one or more competitor variants:
  - Drop disabled competitors and out-of-stock observations.
  - Take the LATEST CompetitorPriceObservation per competitor variant.
  - Write competitorCount + min/median/max to VariantCompetitorStats.

Then enqueue pricing.decide_for_product for the variant's product. Pricing
decisions are PRODUCT-LEVEL — one suggested price applied to every variant —
so the fan-out targets ShopifyProduct, not ShopifyVariant.

No weight, no freshness decay, no volatility. Those lived in the rule
engine that was dropped in the discovery_pivot migration.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _recompute_for_variant(shop_domain: str, shopify_variant_id: str) -> str | None:
    """Refresh stats for one merchant variant. Returns its productId on success."""
    with get_db() as session:
        rows = session.execute(
            text("""
                SELECT DISTINCT ON (pm."competitorVariantId")
                       pm."competitorVariantId" AS cv_id,
                       cpo.price                AS price,
                       cpo."observedAt"         AS observed_at,
                       cpo."isInStock"          AS in_stock,
                       comp.enabled             AS comp_enabled
                FROM "ProductMatch" pm
                JOIN "ScrapedVariant" sv ON sv.id = pm."competitorVariantId"
                JOIN "ScrapedProduct" sp ON sp.id = sv."productId"
                LEFT JOIN "Competitor" comp
                       ON comp."shopDomain" = pm."shopDomain"
                      AND comp.domain       = sp.domain
                LEFT JOIN "CompetitorPriceObservation" cpo
                       ON cpo."competitorVariantId" = pm."competitorVariantId"
                WHERE pm."shopifyVariantId" = :vid
                  AND pm."shopDomain"       = :sd
                  AND pm."dismissedAt" IS NULL
                ORDER BY pm."competitorVariantId", cpo."observedAt" DESC NULLS LAST
            """),
            {"vid": shopify_variant_id, "sd": shop_domain},
        ).all()

        prices = [
            float(r.price) for r in rows
            if r.price is not None
            and r.in_stock is not False
            and (r.comp_enabled is None or r.comp_enabled is True)
            and float(r.price) > 0
        ]

        product_id_row = session.execute(
            text('SELECT "productId" FROM "ShopifyVariant" WHERE id = :vid'),
            {"vid": shopify_variant_id},
        ).first()
        product_id = product_id_row[0] if product_id_row else None

        if not prices:
            session.execute(
                text("""
                    INSERT INTO "VariantCompetitorStats"
                        ("shopifyVariantId", "shopDomain", "competitorCount",
                         "minPrice", "median", "maxPrice", "lastUpdatedAt")
                    VALUES (:vid, :sd, 0, NULL, NULL, NULL, NOW())
                    ON CONFLICT ("shopifyVariantId") DO UPDATE
                        SET "competitorCount" = 0,
                            "minPrice"        = NULL,
                            "median"          = NULL,
                            "maxPrice"        = NULL,
                            "lastUpdatedAt"   = NOW()
                """),
                {"vid": shopify_variant_id, "sd": shop_domain},
            )
            return product_id

        prices.sort()
        median   = _percentile(prices, 0.5)
        session.execute(
            text("""
                INSERT INTO "VariantCompetitorStats"
                    ("shopifyVariantId", "shopDomain", "competitorCount",
                     "minPrice", "median", "maxPrice", "lastUpdatedAt")
                VALUES (:vid, :sd, :n, :mn, :md, :mx, NOW())
                ON CONFLICT ("shopifyVariantId") DO UPDATE
                    SET "competitorCount" = :n,
                        "minPrice"        = :mn,
                        "median"          = :md,
                        "maxPrice"        = :mx,
                        "lastUpdatedAt"   = NOW()
            """),
            {
                "vid": shopify_variant_id,
                "sd":  shop_domain,
                "n":   len(prices),
                "mn":  prices[0],
                "md":  median,
                "mx":  prices[-1],
            },
        )

    return product_id


@app.task(name="stats.recompute_for_variant", bind=True, max_retries=3, default_retry_delay=30)
def recompute_for_variant(self, shop_domain: str, shopify_variant_id: str):
    """Public entrypoint: refresh one variant + fan out to pricing decision."""
    try:
        product_id = _recompute_for_variant(shop_domain, shopify_variant_id)
    except Exception as exc:
        raise self.retry(exc=exc)

    if product_id:
        app.send_task(
            "pricing.decide_for_product",
            args=[shop_domain, product_id],
            queue="pricing_queue",
        )
    return {"ok": True, "product_id": product_id}


@app.task(name="stats.recompute_after_observation", bind=True, max_retries=3, default_retry_delay=30)
def recompute_after_observation(self, shop_domain: str, competitor_variant_id: str):
    """Triggered from extractor when a new observation lands.

    v1 flow uses product-level matches (ProductLevelMatch). We resolve the
    competitor variant up to its merchant product via that table and enqueue
    the product-level pricing decision directly. We also refresh per-variant
    stats opportunistically via ProductMatch (legacy variant matches) so
    dashboards stay populated when both edge types exist.
    """
    try:
        with get_db() as session:
            # Product-level fan-out (the v1 trigger that actually fires pricing).
            product_rows = session.execute(
                text("""
                    SELECT DISTINCT plm."shopifyProductId"
                    FROM   "ScrapedVariant"   sv
                    JOIN   "ProductLevelMatch" plm
                           ON plm."scrapedProductId" = sv."productId"
                          AND plm."shopDomain"       = :sd
                          AND plm."rejectedByMerchant" = FALSE
                    WHERE  sv.id = :cv
                """),
                {"cv": competitor_variant_id, "sd": shop_domain},
            ).all()
            product_ids = [r[0] for r in product_rows]

            # Variant-level fan-out (only matters if matcher_svc has run).
            variant_rows = session.execute(
                text("""
                    SELECT DISTINCT pm."shopifyVariantId"
                    FROM "ProductMatch" pm
                    WHERE pm."competitorVariantId" = :cv
                      AND pm."shopDomain"         = :sd
                      AND pm."dismissedAt" IS NULL
                """),
                {"cv": competitor_variant_id, "sd": shop_domain},
            ).all()
            variant_ids = [r[0] for r in variant_rows]
    except Exception as exc:
        raise self.retry(exc=exc)

    for pid in product_ids:
        app.send_task(
            "pricing.decide_for_product",
            args=[shop_domain, pid],
            queue="pricing_queue",
        )
    for vid in variant_ids:
        app.send_task(
            "stats.recompute_for_variant",
            args=[shop_domain, vid],
            queue="stats_queue",
        )
    return {"ok": True, "products": len(product_ids), "variants": len(variant_ids)}
