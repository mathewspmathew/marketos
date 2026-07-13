"""Integration test for pause_dynamic_pricing against the real dev DB."""
import uuid

import pytest

from services.common.db import get_db
from services.common import models
from services.common.pane_config import pause_dynamic_pricing


@pytest.fixture
def seeded_enabled_product():
    shop = f"pause-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id,
            shopDomain=shop,
            title="Test Product",
            dynamicPricingEnabled=True,
            pricingTier="PREMIUM",
            minPriceOverride=800,
            maxPriceOverride=1200,
            frequencyUnit="hour",
            frequencyInterval=6,
        ))

    yield product_id

    with get_db() as s:
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_pause_disables_flag_only(seeded_enabled_product):
    product_id = seeded_enabled_product
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        result = pause_dynamic_pricing(s, product)
        assert result == {"dynamicPricingEnabled": {"old": True, "new": False}}

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.dynamicPricingEnabled is False
        # Everything else must be untouched.
        assert product.pricingTier == "PREMIUM"
        assert float(product.minPriceOverride) == 800.0
        assert float(product.maxPriceOverride) == 1200.0
        assert product.frequencyUnit == "hour"
        assert product.frequencyInterval == 6
