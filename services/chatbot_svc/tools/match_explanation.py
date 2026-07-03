"""Match explanation tool - show why a competitor was matched to a merchant variant.

Queries existing tables (no schema changes):
- ProductMatch: matchScore, vectorDistance, confidenceTier, matchedAt, competitorVariantId
- ScrapedVariant: title, url
- CompetitorCandidate: count how many candidates exist for comparison
"""
from __future__ import annotations

from sqlalchemy import text as sa_text

from services.common.db import get_db
from services.common.models import ShopifyVariant
from services.chatbot_svc.schemas import MatchExplanation


def explain_product_match(
    shop_domain: str,
    variant_id: str,
) -> MatchExplanation | None:
    """Explain how a competitor variant was matched to a merchant variant.

    Returns the latest match, confidence tier, vector similarity, and explanation.
    Includes count of other candidates evaluated during discovery.

    Returns None if no match exists for this variant.
    """
    with get_db() as s:
        # Get variant
        variant = s.get(ShopifyVariant, variant_id)
        if not variant:
            return None

        # Get latest match for this variant
        match_sql = """
            SELECT "competitorVariantId", "competitorProdId", "matchScore",
                   "vectorDistance", "confidenceTier", "matchedAt", "textScore", "imageScore"
            FROM "ProductMatch"
            WHERE "shopifyVariantId" = :variant_id
              AND "shopDomain" = :shop
            ORDER BY "matchedAt" DESC
            LIMIT 1
        """
        match_row = s.execute(
            sa_text(match_sql),
            {"variant_id": variant_id, "shop": shop_domain},
        ).mappings().first()

        if not match_row or not match_row["competitorVariantId"]:
            return None

        # Get competitor variant details
        competitor_variant_sql = """
            SELECT "title", "url"
            FROM "ScrapedVariant"
            WHERE "id" = :var_id
        """
        competitor_variant_row = s.execute(
            sa_text(competitor_variant_sql),
            {"var_id": match_row["competitorVariantId"]},
        ).mappings().first()

        if not competitor_variant_row:
            return None

        competitor_title = competitor_variant_row["title"] or "Unknown competitor"
        competitor_url = competitor_variant_row.get("url")

        # Count other candidates for this product (context on discovery scale)
        product_sql = """
            SELECT "shopifyProductId" FROM "ProductMatch"
            WHERE "shopifyVariantId" = :variant_id LIMIT 1
        """
        product_row = s.execute(
            sa_text(product_sql),
            {"variant_id": variant_id},
        ).mappings().first()

        other_candidates_count = 0
        if product_row:
            candidates_sql = """
                SELECT COUNT(*) as cnt FROM "CompetitorCandidate"
                WHERE "shopifyProductId" = :product_id AND "shopDomain" = :shop
            """
            candidates_row = s.execute(
                sa_text(candidates_sql),
                {"product_id": product_row["shopifyProductId"], "shop": shop_domain},
            ).mappings().first()
            other_candidates_count = int(candidates_row["cnt"] or 0)

        # Extract scores
        match_score = float(match_row["matchScore"])
        vector_distance = float(match_row["vectorDistance"])
        confidence_tier = match_row["confidenceTier"] or "WEAK"
        text_score = float(match_row.get("textScore") or 0)
        image_score = float(match_row.get("imageScore") or 0)

        # Build explanation based on confidence and scores
        explanation = _build_match_explanation(
            confidence_tier, match_score, text_score, image_score, competitor_title
        )

        return MatchExplanation(
            variant_id=variant_id,
            variant_title=variant.title,
            competitor_variant_title=competitor_title,
            competitor_url=competitor_url,
            confidence_tier=confidence_tier,
            match_score=match_score,
            vector_distance=vector_distance,
            matched_at=match_row["matchedAt"].isoformat(),
            explanation=explanation,
            other_candidates_evaluated=other_candidates_count,
            recommended_action=_get_action(confidence_tier),
        )


def _build_match_explanation(
    confidence_tier: str,
    match_score: float,
    text_score: float,
    image_score: float,
    competitor_title: str,
) -> str:
    """Build merchant-facing explanation based on match quality."""
    score_pct = match_score

    if confidence_tier == "CONFIRMED":
        reason = f"strong semantic and structural similarity ({score_pct:.0f}%)"
        return (
            f"This is a high-confidence match for {competitor_title}. "
            f"The algorithm found {reason}."
        )

    if confidence_tier == "LIKELY":
        parts = []
        if text_score > 0:
            parts.append(f"text similarity {text_score:.0f}%")
        if image_score > 0:
            parts.append(f"visual similarity {image_score:.0f}%")
        score_desc = " and ".join(parts) if parts else f"overall score {score_pct:.0f}%"
        return (
            f"This is a likely match for {competitor_title}. "
            f"Based on {score_desc}."
        )

    # WEAK
    return (
        f"This is a weak match for {competitor_title}. "
        f"Low confidence ({score_pct:.0f}%). "
        f"Verify this competitor actually sells this product."
    )


def _get_action(confidence_tier: str) -> str:
    """Return recommended action based on match confidence."""
    actions = {
        "CONFIRMED": "Use this match for pricing — high confidence.",
        "LIKELY": "Review the match details; likely valid for pricing.",
        "WEAK": "Verify this competitor before using for pricing decisions.",
    }
    return actions.get(confidence_tier, "Review the match confidence level.")
