"""
services/matcher_svc/scoring.py

Confidence scoring and tier classification for ProductMatch rows.

Why a separate file: the matcher already produces a 0-1 hybrid similarity
score (text+image cosine). For dynamic pricing we need a richer "is this
really the same product?" confidence that gates auto-apply. The current
implementation is a heuristic combining:
  - hybrid_sim    (the existing text+image score)
  - brand match   (vendor equality bonus)
  - type match    (productType equality bonus)
  - price plausibility (penalty for >5x price ratio)

The plan calls for swapping this with a cross-encoder reranker
(bge-reranker-base or similar) in a later phase. Call sites should rely on
this module's API, not the internals, so the swap is a one-file change.

Tier cutoffs are deliberately conservative — only CONFIRMED matches gate
auto-apply.
"""
from __future__ import annotations


CONFIRMED_THRESHOLD = 0.85
LIKELY_THRESHOLD    = 0.65

BRAND_BONUS  = 0.10
TYPE_BONUS   = 0.05
PRICE_PENALTY = 0.15
PRICE_RATIO_LIMIT = 5.0  # >5x ratio between merchant/competitor → penalty


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def compute_confidence(
    hybrid_sim: float,
    merchant_vendor: str | None,
    competitor_vendor: str | None,
    merchant_type: str | None,
    competitor_type: str | None,
    merchant_price: float | None,
    competitor_price: float | None,
) -> float:
    """Map hybrid similarity + structured attributes to a [0,1] confidence.

    `hybrid_sim` is the existing α·text_sim + (1-α)·img_sim score in [0,1].
    Bonuses and penalties are deliberately small so the cosine signal still
    dominates; structured attributes are tie-breakers, not overrides.
    """
    score = float(hybrid_sim)

    if merchant_vendor and competitor_vendor:
        if _norm(merchant_vendor) == _norm(competitor_vendor):
            score += BRAND_BONUS

    if merchant_type and competitor_type:
        if _norm(merchant_type) == _norm(competitor_type):
            score += TYPE_BONUS

    if merchant_price and competitor_price and merchant_price > 0 and competitor_price > 0:
        ratio = max(merchant_price, competitor_price) / min(merchant_price, competitor_price)
        if ratio > PRICE_RATIO_LIMIT:
            score -= PRICE_PENALTY

    return max(0.0, min(1.0, score))


def confidence_tier(confidence: float) -> str:
    """Return CONFIRMED / LIKELY / WEAK based on configured thresholds."""
    if confidence >= CONFIRMED_THRESHOLD:
        return "CONFIRMED"
    if confidence >= LIKELY_THRESHOLD:
        return "LIKELY"
    return "WEAK"
