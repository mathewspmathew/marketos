import uuid
from datetime import datetime, timezone

from services.common.db import get_db
from services.common import models


def test_dynamic_pricing_configured_at_column_exists_and_is_nullable():
    shop = f"migration-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Migration Test Product",
            dynamicPricingEnabled=False,
        ))

    try:
        with get_db() as s:
            product = s.get(models.ShopifyProduct, product_id)
            assert product.dynamicPricingConfiguredAt is None

            product.dynamicPricingConfiguredAt = datetime.now(timezone.utc)
            s.flush()

        with get_db() as s:
            product = s.get(models.ShopifyProduct, product_id)
            assert product.dynamicPricingConfiguredAt is not None
    finally:
        with get_db() as s:
            s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
            s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_default_pricing_tier_column_exists_with_competitive_default():
    shop = f"migration-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopSettings(shopDomain=shop))

    try:
        with get_db() as s:
            settings = s.get(models.ShopSettings, shop)
            assert settings.defaultPricingTier == "COMPETITIVE"

            settings.defaultPricingTier = "PREMIUM"
            s.flush()

        with get_db() as s:
            settings = s.get(models.ShopSettings, shop)
            assert settings.defaultPricingTier == "PREMIUM"
    finally:
        with get_db() as s:
            s.query(models.ShopSettings).filter(models.ShopSettings.shopDomain == shop).delete(synchronize_session=False)
            s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)
