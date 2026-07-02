"""Price explanation tool - use stored reasoning from PriceDecision.

Queries existing tables (no schema changes):
- PriceDecision: newPrice, oldPrice, reason, decidedAt, competitorsUsed
- VariantCompetitorStats: median, minPrice, maxPrice, competitorCount, lastUpdatedAt
- ShopifyVariant: currentPrice, title
"""
from __future__ import annotations

from sqlalchemy import text as sa_text

from services.common.db import get_db
from services.common.models import ShopifyVariant
from services.chatbot_svc.schemas import (
    PriceExplanation,
    CompetitorPricingContext,
)


def explain_price_decision(
    shop_domain: str,
    variant_id: str,
) -> PriceExplanation | None:
    """Explain why a price was recommended for a variant.

    Uses the stored `reason` field from PriceDecision (populated by pricing-worker).
    Supplements with competitor stats context from VariantCompetitorStats.

    Returns None if no price decision exists for this variant.
    """
    with get_db() as s:
        # Get variant
        variant = s.get(ShopifyVariant, variant_id)
        if not variant:
            return None

        # Get latest price decision (has stored reasoning)
        decision_sql = """
            SELECT "newPrice", "oldPrice", "reason", "decidedAt", "competitorsUsed", "skipReason"
            FROM "PriceDecision"
            WHERE "shopifyVariantId" = :variant_id
              AND "shopDomain" = :shop
            ORDER BY "decidedAt" DESC
            LIMIT 1
        """
        decision_row = s.execute(
            sa_text(decision_sql),
            {"variant_id": variant_id, "shop": shop_domain},
        ).mappings().first()

        if not decision_row:
            return None

        # Get competitor stats for context (use CORRECT column names)
        stats_sql = """
            SELECT "median", "minPrice", "maxPrice", "competitorCount", "lastUpdatedAt"
            FROM "VariantCompetitorStats"
            WHERE "shopifyVariantId" = :variant_id
        """
        stats_row = s.execute(
            sa_text(stats_sql),
            {"variant_id": variant_id},
        ).mappings().first()

        # Current and recommended prices
        current = float(variant.currentPrice or 0)
        recommended = float(decision_row["newPrice"])
        old_price = float(decision_row["oldPrice"] or current)
        delta = recommended - current
        delta_percent = ((recommended / current) - 1) * 100 if current > 0 else 0

        # Use stored reason from PriceDecision
        stored_reason = decision_row["reason"] or ""
        competitors_used = int(decision_row["competitorsUsed"] or 0)
        skip_reason = decision_row["skipReason"]

        # Build competitor context if available
        competitor_context = None
        primary_factor = "price_decision"

        if stats_row and stats_row["median"]:
            median = float(stats_row["median"])
            min_price = float(stats_row["minPrice"] or 0)
            max_price = float(stats_row["maxPrice"] or 0)
            count = int(stats_row["competitorCount"] or 0)
            last_updated_raw = stats_row["lastUpdatedAt"]
            last_updated = (
                last_updated_raw.isoformat() if last_updated_raw else None
            )

            competitor_context = CompetitorPricingContext(
                median=median,
                mean=0.0,  # Not available in current schema
                min_price=min_price,
                max_price=max_price,
                count=count,
                last_observed=last_updated,
            )

            primary_factor = "competitor_median"

        # Build merchant-facing explanation
        if skip_reason:
            explanation = (
                f"Price decision was skipped: {skip_reason}. "
                "Recommendation: "
                + _get_skip_action(skip_reason)
            )
        else:
            explanation = stored_reason or _build_fallback_explanation(
                current, recommended, delta, delta_percent, competitors_used
            )

        return PriceExplanation(
            variant_id=variant_id,
            variant_title=variant.title,
            current_price=current,
            recommended_price=recommended,
            price_delta=delta,
            delta_percent=delta_percent,
            decided_at=decision_row["decidedAt"].isoformat(),
            competitor_pricing=competitor_context,
            primary_factor=primary_factor,
            explanation=explanation,
        )


def _get_skip_action(skip_reason: str) -> str:
    """Return actionable next step based on skip reason."""
    skip_actions = {
        "below_min_competitors": "Wait for more competitors to be discovered, or lower the minimum competitor threshold.",
        "no_matches": "Check if discovery found competitors but matching failed. Adjust search query or verification settings.",
        "oos_all": "Product is out of stock. Price will be recommended once inventory is available.",
        "currency_mismatch": "Competitor currency doesn't match. Check competitor product data.",
    }
    return skip_actions.get(skip_reason, "Check pricing configuration and try again.")


def _build_fallback_explanation(
    current: float,
    recommended: float,
    delta: float,
    delta_percent: float,
    competitors_used: int,
) -> str:
    """Build explanation from price delta if stored reason is unavailable."""
    if abs(delta) < 0.01:
        return (
            f"Price recommendation is ${recommended:.2f}, matching your current price. "
            f"Based on {competitors_used} competitor(s)."
        )

    direction = "increase" if delta > 0 else "decrease"
    return (
        f"Recommended price is ${recommended:.2f}, a {direction} of ${abs(delta):.2f} "
        f"({abs(delta_percent):.1f}%) from your current ${current:.2f}. "
        f"Based on {competitors_used} competitor(s)."
    )
