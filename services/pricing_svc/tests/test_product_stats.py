"""product_stats.py mirrors the Stats pages' data needs, computed server-side
so browser and chatbot clients see identical numbers — see
test_decide_confirmed_likely_gate.py for the gate this reuses."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.common.db import get_db
from services.common import models
from services.pricing_svc.product_stats import (
    _clamp_explanation, get_match_activity, get_product_stats, list_product_stats,
)


def test_clamp_explanation_per_round():
    result = _clamp_explanation("clamped_per_round", "ref=76.38 target=74.85 tier=COMPETITIVE comps=3", 76.38)
    assert result["line1"] == "Target ₹74.85 → ₹76.38 (per-round cap)"
    assert "Limited by maximum change per cycle" in result["line2"]
    assert "Reference: ₹76.38" in result["line2"]
    assert "3 competitors" in result["line2"]
    assert "COMPETITIVE" in result["line2"]


def test_clamp_explanation_lifetime_cap():
    result = _clamp_explanation("clamped_lifetime_cap", "ref=120.00 target=132.00 tier=PREMIUM comps=5", 110.00)
    assert result["line1"] == "Target ₹132.00 → ₹110.00 (lifetime cap)"
    assert "Price adjusted to stay within allowed range" in result["line2"]
    assert "Reference: ₹120.00" in result["line2"]
    assert "5 competitors" in result["line2"]
    assert "PREMIUM" in result["line2"]


def test_clamp_explanation_none_when_no_clamp_reason():
    assert _clamp_explanation(None, "ref=76.38 target=74.85 tier=COMPETITIVE comps=3", 76.38) is None


def test_clamp_explanation_none_when_reason_malformed():
    assert _clamp_explanation("clamped_per_round", "malformed reason string", 76.38) is None


def test_clamp_explanation_none_for_unknown_clamp_type():
    assert _clamp_explanation("clamped_unknown", "ref=76.38 target=74.85 tier=COMPETITIVE comps=3", 76.38) is None


@pytest.fixture
def seeded_shop():
    shop = f"product-stats-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopSettings(
            shopDomain=shop, markupPct=0.1, minCompetitorsToPrice=2,
            topKCompetitors=3, maxAutoApplyChangePct=0.5, lifetimeCapPct=0.5,
            budgetUndercut=0.05, premiumUplift=0.05, includeOosInPricing=False,
            minChangePctThreshold=0.001, minFreshnessHours=999999,
        ))
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Product Stats Test Product",
            dynamicPricingEnabled=True, pricingTier="COMPETITIVE",
            lastDecisionAt=now,
        ))
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default",
            currentPrice=100.00, basePrice=100.00, updatedAt=now,
        ))

    yield shop, product_id, variant_id

    with get_db() as s:
        s.query(models.PriceDecision).filter(models.PriceDecision.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.CompetitorPriceObservation).filter(
            models.CompetitorPriceObservation.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ProductLevelMatch).filter(models.ProductLevelMatch.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopSettings).filter(models.ShopSettings.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def _add_match(session, shop, product_id, *, tier, confirmed, price=Decimal("80.00"), with_observation=True):
    scraped_id = str(uuid.uuid4())
    scraped_variant_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    session.add(models.ScrapedProduct(id=scraped_id, shopDomain=shop, domain="comp.example.com", title=f"Competitor {tier}"))
    session.flush()
    session.add(models.ScrapedVariant(
        id=scraped_variant_id, productId=scraped_id, title="Default",
        currentPrice=price, currency="INR", updatedAt=now,
    ))
    session.add(models.ProductLevelMatch(
        id=str(uuid.uuid4()), shopDomain=shop,
        shopifyProductId=product_id, scrapedProductId=scraped_id,
        confidence=0.9 if tier == "CONFIRMED" else 0.7,
        confidenceTier=tier,
        reviewStatus="CONFIRMED" if confirmed else "PENDING",
        updatedAt=now,
    ))
    if with_observation:
        session.flush()
        session.add(models.CompetitorPriceObservation(
            id=str(uuid.uuid4()), shopDomain=shop, competitorVariantId=scraped_variant_id,
            price=price, currency="INR", isInStock=True, observedAt=now,
        ))


def test_strong_match_count_uses_same_gate_as_decide(seeded_shop):
    shop, product_id, _variant_id = seeded_shop
    with get_db() as s:
        _add_match(s, shop, product_id, tier="CONFIRMED", confirmed=False)
        _add_match(s, shop, product_id, tier="LIKELY", confirmed=True)
        _add_match(s, shop, product_id, tier="LIKELY", confirmed=False)  # excluded
        _add_match(s, shop, product_id, tier="WEAK", confirmed=True)     # excluded

    with get_db() as s:
        result = get_product_stats(s, shop, product_id)

    assert result["waiting"] == {"have": 2, "need": 2}
    assert len(result["competitorSeries"]) == 2


def test_unknown_product_raises_value_error(seeded_shop):
    shop, _product_id, _variant_id = seeded_shop
    with get_db() as s:
        with pytest.raises(ValueError):
            get_product_stats(s, shop, "gid://shopify/Product/does-not-exist")


def test_decision_status_and_clamp_explanation(seeded_shop):
    shop, product_id, variant_id = seeded_shop
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(models.PriceDecision(
            id=str(uuid.uuid4()), shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=Decimal("100.00"), newPrice=Decimal("95.00"),
            reason="ref=90.00 target=90.00 tier=COMPETITIVE comps=3",
            skipReason="clamped_per_round", autoApplied=True, appliedAt=now, decidedAt=now,
        ))

    with get_db() as s:
        result = get_product_stats(s, shop, product_id)

    d = result["decisions"][0]
    assert d["status"] == "applied"
    assert d["clampReason"] == "clamped_per_round"
    assert d["skipReason"] is None
    assert d["clampExplanation"]["line1"] == "Target ₹90.00 → ₹95.00 (per-round cap)"


def test_list_product_stats_includes_computed_status(seeded_shop):
    shop, product_id, variant_id = seeded_shop
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(models.PriceDecision(
            id=str(uuid.uuid4()), shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=Decimal("100.00"), newPrice=Decimal("95.00"),
            reason="test", applyError="Shopify rejected the write", decidedAt=now,
        ))

    with get_db() as s:
        result = list_product_stats(s, shop)

    row = next(r for r in result if r["id"] == product_id)
    assert row["lastStatus"] == "failed"


def test_list_product_stats_excludes_products_without_decisions(seeded_shop):
    shop, _product_id, _variant_id = seeded_shop
    other_product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    with get_db() as s:
        s.add(models.ShopifyProduct(
            id=other_product_id, shopDomain=shop, title="No decisions yet",
            dynamicPricingEnabled=True,
        ))

    try:
        with get_db() as s:
            result = list_product_stats(s, shop)
        assert other_product_id not in [r["id"] for r in result]
    finally:
        with get_db() as s:
            s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == other_product_id).delete(synchronize_session=False)


def test_get_match_activity_includes_created_confirmed_and_rejected(seeded_shop):
    shop, product_id, _variant_id = seeded_shop
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)

    with get_db() as s:
        scraped_confirmed = str(uuid.uuid4())
        scraped_rejected = str(uuid.uuid4())
        s.add(models.ScrapedProduct(id=scraped_confirmed, shopDomain=shop, domain="confirmed.example.com", title="Confirmed Competitor"))
        s.add(models.ScrapedProduct(id=scraped_rejected, shopDomain=shop, domain="rejected.example.com", title="Rejected Competitor"))
        s.flush()
        s.add(models.ProductLevelMatch(
            id=str(uuid.uuid4()), shopDomain=shop, shopifyProductId=product_id,
            scrapedProductId=scraped_confirmed, confidence=0.9, confidenceTier="CONFIRMED",
            reviewStatus="CONFIRMED", createdAt=now, reviewedAt=now, updatedAt=now,
        ))
        s.add(models.ProductLevelMatch(
            id=str(uuid.uuid4()), shopDomain=shop, shopifyProductId=product_id,
            scrapedProductId=scraped_rejected, confidence=0.7, confidenceTier="LIKELY",
            reviewStatus="REJECTED", createdAt=now, reviewedAt=now, updatedAt=now,
        ))

    with get_db() as s:
        events = get_match_activity(s, shop, product_id, since)

    types = {e["type"] for e in events}
    # Each match yields both a "created" event and a review-status event.
    assert types == {"created", "confirmed", "rejected"}
    rejected = [e for e in events if e["type"] == "rejected"]
    assert rejected and "Rejected Competitor" in rejected[0]["description"]
