"""_recompute_for_variant must use the same usable-match gate as decide.py's
product-level eligibility check (CONFIRMED unconditionally, LIKELY only once
merchant-confirmed) — see test_decide_confirmed_likely_gate.py for the
product-level equivalent this mirrors."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services.common.db import get_db
from services.common import models
from services.pricing_svc.stats import _recompute_for_variant


@pytest.fixture
def seeded_variant():
    shop = f"stats-gate-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Stats Gate Test Product",
            dynamicPricingEnabled=True,
        ))
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default",
            currentPrice=100.00, updatedAt=datetime.now(timezone.utc),
        ))

    yield shop, product_id, variant_id

    with get_db() as s:
        s.query(models.VariantCompetitorStats).filter(
            models.VariantCompetitorStats.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.CompetitorPriceObservation).filter(
            models.CompetitorPriceObservation.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ProductMatch).filter(models.ProductMatch.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ProductLevelMatch).filter(models.ProductLevelMatch.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def _add_competitor(session, shop, product_id, variant_id, *, tier, confirmed, price=Decimal("80.00")):
    scraped_id = str(uuid.uuid4())
    scraped_variant_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    session.add(models.ScrapedProduct(
        id=scraped_id, shopDomain=shop, domain="comp.example.com", title=f"Competitor {tier}",
    ))
    session.flush()
    session.add(models.ScrapedVariant(
        id=scraped_variant_id, productId=scraped_id, title="Default",
        currentPrice=price, currency="INR", updatedAt=now,
    ))
    plm = models.ProductLevelMatch(
        id=str(uuid.uuid4()), shopDomain=shop,
        shopifyProductId=product_id, scrapedProductId=scraped_id,
        confidence=0.9 if tier == "CONFIRMED" else 0.7,
        confidenceTier=tier,
        reviewStatus="CONFIRMED" if confirmed else "PENDING",
        updatedAt=now,
    )
    session.add(plm)
    session.flush()
    session.add(models.ProductMatch(
        id=str(uuid.uuid4()), shopDomain=shop,
        shopifyVariantId=variant_id, competitorVariantId=scraped_variant_id,
        competitorProdId=scraped_id, matchScore=Decimal("90.00"),
        vectorDistance=Decimal("0.100000"), thresholdUsed=Decimal("0.5000"),
        confidenceTier=tier, productMatchId=plm.id, updatedAt=now,
    ))
    session.flush()
    session.add(models.CompetitorPriceObservation(
        id=str(uuid.uuid4()), shopDomain=shop, competitorVariantId=scraped_variant_id,
        price=price, currency="INR", isInStock=True, observedAt=now,
    ))


def test_confirmed_match_counts_without_merchant_confirmation(seeded_variant):
    shop, product_id, variant_id = seeded_variant
    with get_db() as s:
        _add_competitor(s, shop, product_id, variant_id, tier="CONFIRMED", confirmed=False)

    _recompute_for_variant(shop, variant_id)

    with get_db() as s:
        stats = s.get(models.VariantCompetitorStats, variant_id)
        assert stats.competitorCount == 1


def test_likely_match_confirmed_by_merchant_counts(seeded_variant):
    shop, product_id, variant_id = seeded_variant
    with get_db() as s:
        _add_competitor(s, shop, product_id, variant_id, tier="LIKELY", confirmed=True)

    _recompute_for_variant(shop, variant_id)

    with get_db() as s:
        stats = s.get(models.VariantCompetitorStats, variant_id)
        assert stats.competitorCount == 1


def test_likely_match_unconfirmed_is_excluded(seeded_variant):
    shop, product_id, variant_id = seeded_variant
    with get_db() as s:
        _add_competitor(s, shop, product_id, variant_id, tier="LIKELY", confirmed=False)

    _recompute_for_variant(shop, variant_id)

    with get_db() as s:
        stats = s.get(models.VariantCompetitorStats, variant_id)
        assert stats.competitorCount == 0


def test_weak_match_stays_excluded_regardless_of_confirmation(seeded_variant):
    shop, product_id, variant_id = seeded_variant
    with get_db() as s:
        _add_competitor(s, shop, product_id, variant_id, tier="WEAK", confirmed=True)

    _recompute_for_variant(shop, variant_id)

    with get_db() as s:
        stats = s.get(models.VariantCompetitorStats, variant_id)
        assert stats.competitorCount == 0


def test_rejected_match_stays_excluded_even_if_confirmed(seeded_variant):
    shop, product_id, variant_id = seeded_variant
    with get_db() as s:
        _add_competitor(s, shop, product_id, variant_id, tier="LIKELY", confirmed=True)
        s.query(models.ProductLevelMatch).filter(
            models.ProductLevelMatch.shopifyProductId == product_id
        ).update({"reviewStatus": "REJECTED"})

    _recompute_for_variant(shop, variant_id)

    with get_db() as s:
        stats = s.get(models.VariantCompetitorStats, variant_id)
        assert stats.competitorCount == 0
