from __future__ import annotations

import enum

from sqlalchemy import text as sa_text

from services.chatbot_svc.schemas import ScopeFilter
from services.common.db import get_db


class StatsMetric(str, enum.Enum):
    catalog_summary = "catalog_summary"
    priced_above_competitor = "priced_above_competitor"
    priced_below_competitor = "priced_below_competitor"
    match_coverage = "match_coverage"
    recent_decisions = "recent_decisions"


# Column notes (verified from shopify_ui/prisma/schema.prisma):
#   VariantCompetitorStats.shopifyVariantId  — PK (not "variantId")
#   VariantCompetitorStats.median            — median competitor price column
#   ProductMatch.shopifyVariantId            — FK to ShopifyVariant
#   PriceDecision.oldPrice / newPrice        — a decision is a no-op reaffirmation
#                                               when oldPrice == newPrice; only
#                                               oldPrice != newPrice rows are a
#                                               real price change
#   PriceDecision.decidedAt                  — timestamp column (not "createdAt")

RECENT_DECISIONS_WINDOW = "7 days"

_QUERIES: dict[StatsMetric, str] = {
    StatsMetric.catalog_summary: """
        SELECT
          (SELECT COUNT(*)
             FROM "ShopifyProduct"
             WHERE "shopDomain" = :shop) AS products,
          (SELECT COUNT(*)
             FROM "ShopifyVariant" v
             JOIN "ShopifyProduct" p ON p.id = v."productId"
             WHERE p."shopDomain" = :shop) AS variants
    """,
    StatsMetric.priced_above_competitor: """
        SELECT COUNT(*) AS count
        FROM "ShopifyVariant" v
        JOIN "ShopifyProduct" p ON p.id = v."productId"
        JOIN "VariantCompetitorStats" s ON s."shopifyVariantId" = v.id
        WHERE p."shopDomain" = :shop
          AND COALESCE(v."currentPrice", 0) > s."median"
    """,
    StatsMetric.priced_below_competitor: """
        SELECT COUNT(*) AS count
        FROM "ShopifyVariant" v
        JOIN "ShopifyProduct" p ON p.id = v."productId"
        JOIN "VariantCompetitorStats" s ON s."shopifyVariantId" = v.id
        WHERE p."shopDomain" = :shop
          AND COALESCE(v."currentPrice", 0) < s."median"
    """,
    StatsMetric.match_coverage: """
        SELECT
          (SELECT COUNT(DISTINCT pm."shopifyVariantId")
             FROM "ProductMatch" pm
             WHERE pm."shopDomain" = :shop) AS matched,
          (SELECT COUNT(*)
             FROM "ShopifyVariant" v3
             JOIN "ShopifyProduct" p3 ON p3.id = v3."productId"
             WHERE p3."shopDomain" = :shop) AS total
    """,
    StatsMetric.recent_decisions: f"""
        WITH recent AS (
            SELECT id, "shopifyVariantId", "oldPrice", "newPrice", "decidedAt"
            FROM "PriceDecision"
            WHERE "shopDomain" = :shop
              AND "decidedAt" >= now() - interval '{RECENT_DECISIONS_WINDOW}'
        ),
        changed AS (
            SELECT * FROM recent WHERE "oldPrice" <> "newPrice"
        )
        SELECT
          (SELECT COUNT(*) FROM recent) AS total_decisions,
          (SELECT COUNT(*) FROM changed) AS price_changes,
          (SELECT MIN("newPrice") FROM changed) AS min_changed_price,
          (SELECT MAX("newPrice") FROM changed) AS max_changed_price,
          (SELECT json_agg(c) FROM (
             SELECT "shopifyVariantId", "oldPrice", "newPrice", "decidedAt"
             FROM changed
             ORDER BY "decidedAt" DESC
             LIMIT 5
           ) c) AS sample_changes
    """,
}


def get_stats(
    shop_domain: str,
    metric: StatsMetric,
    scope: ScopeFilter | None = None,  # reserved for future per-scope filtering
) -> dict:
    """Run a canned read-only analytical query and return a typed dict.

    No mutations are performed. ``scope`` is accepted for API symmetry but not
    yet applied — all queries are scoped only to ``shop_domain``.
    """
    sql = _QUERIES[metric]
    with get_db() as s:
        rows = s.execute(sa_text(sql), {"shop": shop_domain}).mappings().all()

    if metric == StatsMetric.catalog_summary:
        r = rows[0]
        return {"products": int(r["products"]), "variants": int(r["variants"])}

    if metric in (StatsMetric.priced_above_competitor, StatsMetric.priced_below_competitor):
        return {"count": int(rows[0]["count"])}

    if metric == StatsMetric.match_coverage:
        r = rows[0]
        return {"matched": int(r["matched"]), "total": int(r["total"])}

    if metric == StatsMetric.recent_decisions:
        r = rows[0]
        price_changes = int(r["price_changes"])
        total = int(r["total_decisions"])
        return {
            "window": RECENT_DECISIONS_WINDOW,
            "total_decisions": total,
            "price_changes": price_changes,
            "no_op_reaffirmations": total - price_changes,
            "changed_price_range": (
                {"min": float(r["min_changed_price"]), "max": float(r["max_changed_price"])}
                if price_changes > 0
                else None
            ),
            "sample_changes": r["sample_changes"] or [],
        }

    return {"rows": [dict(r) for r in rows]}
