import uuid
from datetime import datetime, timezone

from services.common.db import get_db
from services.common import models


def test_product_level_match_review_status_round_trips():
    shop = f"plm-review-status-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    scraped_id = str(uuid.uuid4())
    match_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(id=product_id, shopDomain=shop, title="Review Status Test"))
        s.add(models.ScrapedProduct(
            id=scraped_id, shopDomain=shop, domain="comp.example.com", title="Competitor",
        ))
        s.flush()
        s.add(models.ProductLevelMatch(
            id=match_id, shopDomain=shop,
            shopifyProductId=product_id, scrapedProductId=scraped_id,
            confidence=0.7, confidenceTier="LIKELY", reviewStatus="CONFIRMED",
        ))

    with get_db() as s:
        row = s.get(models.ProductLevelMatch, match_id)
        assert row.reviewStatus == "CONFIRMED"

    with get_db() as s:
        s.query(models.ProductLevelMatch).filter(models.ProductLevelMatch.id == match_id).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_product_level_match_review_status_defaults_to_pending():
    shop = f"plm-review-status-default-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    scraped_id = str(uuid.uuid4())
    match_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(id=product_id, shopDomain=shop, title="Review Status Default Test"))
        s.add(models.ScrapedProduct(
            id=scraped_id, shopDomain=shop, domain="comp.example.com", title="Competitor",
        ))
        s.flush()
        s.add(models.ProductLevelMatch(
            id=match_id, shopDomain=shop,
            shopifyProductId=product_id, scrapedProductId=scraped_id,
            confidence=0.7, confidenceTier="LIKELY",
        ))

    with get_db() as s:
        row = s.get(models.ProductLevelMatch, match_id)
        assert row.reviewStatus == "PENDING"

    with get_db() as s:
        s.query(models.ProductLevelMatch).filter(models.ProductLevelMatch.id == match_id).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)
