import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.common.db import get_db
from services.common import models
from services.pricing_svc.decide import decide_price_for_product


@pytest.fixture
def seeded_shop():
    shop = f"likely-gate-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopSettings(
            shopDomain=shop, markupPct=0.1, minCompetitorsToPrice=1,
            topKCompetitors=3, maxAutoApplyChangePct=0.5, lifetimeCapPct=0.5,
            budgetUndercut=0.05, premiumUplift=0.05, includeOosInPricing=False,
            minChangePctThreshold=0.001, minFreshnessHours=999999,
        ))
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Likely Gate Test Product",
            dynamicPricingEnabled=True, syncPrice=True, pricingTier="COMPETITIVE",
        ))
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default",
            currentPrice=100.00, basePrice=100.00,
        ))

    yield shop, product_id, variant_id

    with get_db() as s:
        s.query(models.CompetitorPriceObservation).filter(
            models.CompetitorPriceObservation.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ProductLevelMatch).filter(
            models.ProductLevelMatch.shopDomain == shop).delete(synchronize_session=False)
        # ScrapedVariant rows cascade-delete (ondelete="CASCADE") when their
        # parent ScrapedProduct is deleted below.
        s.query(models.ScrapedProduct).filter(
            models.ScrapedProduct.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopSettings).filter(models.ShopSettings.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def _add_competitor(session, shop, product_id, *, tier, confirmed, price=Decimal("80.00")):
    scraped_id = str(uuid.uuid4())
    scraped_variant_id = str(uuid.uuid4())
    session.add(models.ScrapedProduct(
        id=scraped_id, shopDomain=shop, domain="comp.example.com", title=f"Competitor {tier}",
    ))
    session.flush()
    session.add(models.ScrapedVariant(
        id=scraped_variant_id, productId=scraped_id, title="Default",
        currentPrice=price, currency="INR",
    ))
    session.add(models.ProductLevelMatch(
        id=str(uuid.uuid4()), shopDomain=shop,
        shopifyProductId=product_id, scrapedProductId=scraped_id,
        confidence=0.9 if tier == "CONFIRMED" else 0.7,
        confidenceTier=tier,
        reviewStatus="CONFIRMED" if confirmed else "PENDING",
    ))
    session.flush()
    session.add(models.CompetitorPriceObservation(
        id=str(uuid.uuid4()), shopDomain=shop, competitorVariantId=scraped_variant_id,
        price=price, currency="INR", isInStock=True,
        observedAt=datetime.now(timezone.utc),
    ))


def test_confirmed_match_counts_without_merchant_confirmation(seeded_shop):
    shop, product_id, variant_id = seeded_shop
    with get_db() as s:
        _add_competitor(s, shop, product_id, tier="CONFIRMED", confirmed=False)

    result = decide_price_for_product(shop, product_id)

    assert result["ok"] is True
    assert result["competitors"] == 1


def test_likely_match_confirmed_by_merchant_counts(seeded_shop):
    shop, product_id, variant_id = seeded_shop
    with get_db() as s:
        _add_competitor(s, shop, product_id, tier="LIKELY", confirmed=True)

    result = decide_price_for_product(shop, product_id)

    assert result["ok"] is True
    assert result["competitors"] == 1


def test_likely_match_unconfirmed_is_excluded(seeded_shop):
    shop, product_id, variant_id = seeded_shop
    with get_db() as s:
        _add_competitor(s, shop, product_id, tier="LIKELY", confirmed=False)

    result = decide_price_for_product(shop, product_id)

    assert result["ok"] is False
    assert result["reason"] == "below_min_competitors"
    assert result["have"] == 0


def test_weak_match_stays_excluded_regardless_of_confirmation(seeded_shop):
    shop, product_id, variant_id = seeded_shop
    with get_db() as s:
        _add_competitor(s, shop, product_id, tier="WEAK", confirmed=True)

    result = decide_price_for_product(shop, product_id)

    assert result["ok"] is False
    assert result["reason"] == "below_min_competitors"
    assert result["have"] == 0


def test_rejected_match_stays_excluded_even_if_confirmed(seeded_shop):
    shop, product_id, variant_id = seeded_shop
    with get_db() as s:
        _add_competitor(s, shop, product_id, tier="LIKELY", confirmed=True)
        s.query(models.ProductLevelMatch).filter(
            models.ProductLevelMatch.shopifyProductId == product_id
        ).update({"reviewStatus": "REJECTED"})

    result = decide_price_for_product(shop, product_id)

    assert result["ok"] is False
    assert result["reason"] == "below_min_competitors"
    assert result["have"] == 0
