"""MATCH_TIER_LOWEST: match the lowest in-stock price within a tier filter.

Tiers come from Competitor.tier (PREMIUM|MIDMARKET|BUDGET) and the rule's
`tierFilter` column. If tierFilter is empty we fall back to all tiers.

Unlike MATCH_LOWEST (which reads precomputed weightedMin), this queries
CompetitorPriceObservation joined with Competitor + ProductMatch, because
VariantCompetitorStats doesn't carry per-tier breakdowns. The query is
cheap — bounded to the variant's matched competitor variants and the most
recent observation per competitor variant.
"""
from __future__ import annotations

from sqlalchemy import text


def decide(ctx: dict, params: dict, session, shop_domain: str) -> tuple[float | None, str]:
    tiers = ctx["ruleTierFilter"]
    if not tiers:
        tiers = ["PREMIUM", "MIDMARKET", "BUDGET"]

    row = session.execute(
        text("""
            WITH latest AS (
                SELECT DISTINCT ON (cpo."competitorVariantId")
                    cpo."competitorVariantId",
                    cpo.price,
                    cpo."isInStock",
                    cpo."observedAt",
                    cpo."competitorId"
                FROM "CompetitorPriceObservation" cpo
                JOIN "ProductMatch" pm
                  ON pm."scrapedVariantId" = cpo."competitorVariantId"
                WHERE pm."shopifyVariantId" = :v
                  AND pm."shopDomain" = :sd
                ORDER BY cpo."competitorVariantId", cpo."observedAt" DESC
            )
            SELECT MIN(l.price) AS p
            FROM latest l
            JOIN "Competitor" c ON c.id = l."competitorId"
            WHERE l."isInStock" = TRUE
              AND c.enabled = TRUE
              AND c.tier::text = ANY(:tiers)
        """),
        {"v": ctx["variantId"], "sd": shop_domain, "tiers": tiers},
    ).first()

    if not row or row.p is None:
        return None, f"match_tier_lowest: no in-stock observations in tiers={tiers}"

    price = float(row.p)
    return price, f"match_tier_lowest: lowest in {tiers} = {price}"
