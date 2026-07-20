import uuid
from datetime import datetime, timezone

import pytest

from services.common.db import get_db
from services.common import models
from services.pricing_svc.match_review import MatchReviewError, confirm_match, reject_match


@pytest.fixture
def seeded_match():
    shop = f"match-review-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    scraped_id = str(uuid.uuid4())
    scraped_variant_id = str(uuid.uuid4())
    match_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(id=product_id, shopDomain=shop, title="Match Review Test"))
        s.add(models.ShopifyVariant(id=variant_id, productId=product_id, title="Default", currentPrice=100.00))
        s.add(models.ScrapedProduct(id=scraped_id, shopDomain=shop, domain="comp.example.com", title="Competitor"))
        s.flush()
        s.add(models.ScrapedVariant(
            id=scraped_variant_id, productId=scraped_id, title="Default",
            currentPrice=90.00, currency="INR",
        ))
        s.add(models.ProductLevelMatch(
            id=match_id, shopDomain=shop,
            shopifyProductId=product_id, scrapedProductId=scraped_id,
            confidence=0.7, confidenceTier="LIKELY", reviewStatus="PENDING",
        ))
        s.flush()
        s.add(models.ProductMatch(
            id=str(uuid.uuid4()), shopDomain=shop,
            shopifyVariantId=variant_id, competitorVariantId=scraped_variant_id,
            competitorProdId=scraped_id, productMatchId=match_id,
            matchScore=90.0, vectorDistance=0.1, thresholdUsed=0.55,
        ))

    yield shop, match_id, variant_id

    with get_db() as s:
        s.query(models.ProductMatch).filter(models.ProductMatch.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.ProductLevelMatch).filter(models.ProductLevelMatch.id == match_id).delete(synchronize_session=False)
        s.query(models.ScrapedVariant).filter(models.ScrapedVariant.id == scraped_variant_id).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_confirm_match_sets_status_and_reviewed_at(seeded_match):
    shop, match_id, variant_id = seeded_match
    with get_db() as s:
        confirm_match(s, shop, match_id)

    with get_db() as s:
        match = s.get(models.ProductLevelMatch, match_id)
        assert match.reviewStatus == "CONFIRMED"
        assert match.reviewedAt is not None


def test_reject_match_sets_status_and_deletes_product_matches(seeded_match):
    shop, match_id, variant_id = seeded_match
    with get_db() as s:
        reject_match(s, shop, match_id)

    with get_db() as s:
        match = s.get(models.ProductLevelMatch, match_id)
        assert match.reviewStatus == "REJECTED"
        assert match.reviewedAt is not None
        remaining = s.query(models.ProductMatch).filter(models.ProductMatch.shopifyVariantId == variant_id).count()
        assert remaining == 0


def test_confirm_rejects_unknown_match(seeded_match):
    shop, match_id, variant_id = seeded_match
    with get_db() as s:
        with pytest.raises(MatchReviewError, match="not found"):
            confirm_match(s, shop, "nonexistent-match")


def test_confirm_rejects_wrong_shop(seeded_match):
    shop, match_id, variant_id = seeded_match
    with get_db() as s:
        with pytest.raises(MatchReviewError, match="not found"):
            confirm_match(s, "other-shop.myshopify.com", match_id)


def test_confirm_match_twice_is_idempotent(seeded_match):
    shop, match_id, variant_id = seeded_match
    with get_db() as s:
        confirm_match(s, shop, match_id)

    with get_db() as s:
        match = s.get(models.ProductLevelMatch, match_id)
        first_reviewed_at = match.reviewedAt

    with get_db() as s:
        result = confirm_match(s, shop, match_id)
        assert result == {"matchId": match_id, "reviewStatus": "CONFIRMED"}

    with get_db() as s:
        match = s.get(models.ProductLevelMatch, match_id)
        assert match.reviewStatus == "CONFIRMED"
        assert match.reviewedAt == first_reviewed_at


def test_reject_match_twice_is_idempotent(seeded_match):
    shop, match_id, variant_id = seeded_match
    with get_db() as s:
        reject_match(s, shop, match_id)

    with get_db() as s:
        match = s.get(models.ProductLevelMatch, match_id)
        first_reviewed_at = match.reviewedAt

    with get_db() as s:
        result = reject_match(s, shop, match_id)
        assert result == {"matchId": match_id, "reviewStatus": "REJECTED"}

    with get_db() as s:
        match = s.get(models.ProductLevelMatch, match_id)
        assert match.reviewStatus == "REJECTED"
        assert match.reviewedAt == first_reviewed_at
        remaining = s.query(models.ProductMatch).filter(models.ProductMatch.shopifyVariantId == variant_id).count()
        assert remaining == 0
