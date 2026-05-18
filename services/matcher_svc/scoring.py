"""
services/matcher_svc/scoring.py

Confidence scoring and tier classification for ProductMatch rows.

Hardening bundle (step 4):
  - PRICE_RATIO_LIMIT is now a HARD reject (returns 0.0), not a soft penalty.
    The SQL pre-filter also rejects on this same ratio, so this is the safety
    net for cases where price was null at SQL time but non-null at scoring time.
  - confidence_tier now takes a `brand_match` flag. CONFIRMED requires BOTH a
    high confidence score AND a brand match (both vendors present and equal).
    Without brand confirmation the maximum tier is LIKELY, regardless of score.
    Reason: embedding similarity alone is not strong enough evidence to auto-
    apply pricing to a product without brand corroboration.
  - ABSOLUTE_HYBRID_FLOOR is exported for the matcher's pre-threshold filter.
"""
from __future__ import annotations


CONFIRMED_THRESHOLD = 0.85
LIKELY_THRESHOLD    = 0.65

BRAND_BONUS  = 0.10
TYPE_BONUS   = 0.05
PRICE_RATIO_LIMIT = 5.0       # >5x ratio between prices → hard reject

# Used by the matcher to drop candidates whose hybrid (text+image) similarity
# is below this floor, regardless of the domain-adaptive threshold. Without
# this floor a domain whose candidates are all weak can still let the
# "best of the bad" through.
ABSOLUTE_HYBRID_FLOOR = 0.55


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def brand_match(merchant_vendor: str | None, competitor_vendor: str | None) -> bool:
    """True only when BOTH sides have a vendor and they are equal (case-insensitive).
    Null on either side counts as 'unknown', not 'match' — needed to gate
    auto-apply behind real brand evidence."""
    if not merchant_vendor or not competitor_vendor:
        return False
    return _norm(merchant_vendor) == _norm(competitor_vendor)


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

    Hard rejects (return 0.0):
      - Price ratio between merchant and competitor exceeds PRICE_RATIO_LIMIT.

    Bonuses:
      - Brand equality      → +BRAND_BONUS
      - Product-type equality → +TYPE_BONUS

    `hybrid_sim` is the existing α·text_sim + (1-α)·img_sim score in [0,1].
    """
    if (merchant_price and competitor_price
            and merchant_price > 0 and competitor_price > 0):
        ratio = max(merchant_price, competitor_price) / min(merchant_price, competitor_price)
        if ratio > PRICE_RATIO_LIMIT:
            return 0.0

    score = float(hybrid_sim)

    if brand_match(merchant_vendor, competitor_vendor):
        score += BRAND_BONUS

    if merchant_type and competitor_type:
        if _norm(merchant_type) == _norm(competitor_type):
            score += TYPE_BONUS

    return max(0.0, min(1.0, score))


def confidence_tier(confidence: float, has_brand_match: bool = False) -> str:
    """Return CONFIRMED / LIKELY / WEAK.

    CONFIRMED requires has_brand_match=True regardless of score — embedding
    similarity alone is not sufficient evidence to auto-apply pricing.
    """
    if confidence >= CONFIRMED_THRESHOLD and has_brand_match:
        return "CONFIRMED"
    if confidence >= LIKELY_THRESHOLD:
        return "LIKELY"
    return "WEAK"
