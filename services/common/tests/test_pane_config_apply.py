"""Integration tests for apply_pane_config against the real dev DB.

Seeds a ShopifyUser + ShopifyProduct + ScrapedProduct + ProductUrl, calls
apply_pane_config, and asserts the resulting row state — no partial writes
on validation failure, correct field writes, and ProductUrl.nextRunAt re-arm.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from services.common.db import get_db
from services.common import models
from services.common.pane_config import PaneConfig, PaneConfigError, apply_pane_config


@pytest.fixture
def seeded_product_with_url():
    shop = f"pane-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    scraped_id = str(uuid.uuid4())
    url_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id,
            shopDomain=shop,
            title="Test Product",
            dynamicPricingEnabled=False,
        ))
        s.add(models.ScrapedProduct(
            id=scraped_id,
            shopDomain=shop,
            domain="example.com",
            title="Competitor Product",
        ))
        s.flush()
        s.add(models.ProductUrl(
            id=url_id,
            shopDomain=shop,
            shopifyProductId=product_id,
            prodId=scraped_id,
            url=f"https://example.com/{url_id}",
            status="ACTIVE",
            nextRunAt=datetime.now(timezone.utc) - timedelta(days=1),
        ))

    yield product_id, url_id

    with get_db() as s:
        s.query(models.ProductUrl).filter(models.ProductUrl.shopifyProductId == product_id).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_apply_writes_fields_and_enables(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        result = apply_pane_config(s, product, PaneConfig(
            pricing_tier="PREMIUM",
            min_price_override=800,
            max_price_override=1200,
            frequency_unit="hour",
            frequency_interval=6,
        ))
        assert result["dynamicPricingEnabled"] == (False, True)
        assert result["rearmedCount"] == 1

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.dynamicPricingEnabled is True
        assert product.pricingTier == "PREMIUM"
        assert float(product.minPriceOverride) == 800.0
        assert float(product.maxPriceOverride) == 1200.0
        assert product.frequencyUnit == "hour"
        assert product.frequencyInterval == 6


def test_apply_rearms_active_product_urls(seeded_product_with_url):
    product_id, url_id = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(pricing_tier="COMPETITIVE", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        url = s.get(models.ProductUrl, url_id)
        # ProductUrl.nextRunAt is a Postgres "timestamp without time zone"
        # column, so reads come back naive even though the model declares
        # DateTime(timezone=True) — compare against a naive UTC "now".
        assert url.nextRunAt > datetime.now(timezone.utc).replace(tzinfo=None)


def test_apply_invalid_bounds_writes_nothing(seeded_product_with_url):
    product_id, url_id = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        with pytest.raises(PaneConfigError):
            apply_pane_config(s, product, PaneConfig(min_price_override=100, max_price_override=50))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        url = s.get(models.ProductUrl, url_id)
        assert product.dynamicPricingEnabled is False
        # See naive-vs-aware note in test_apply_rearms_active_product_urls.
        assert url.nextRunAt < datetime.now(timezone.utc).replace(tzinfo=None)


def test_apply_omitted_fields_leave_existing_values(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        product.searchQueryOverride = "existing override"
        s.flush()
        apply_pane_config(s, product, PaneConfig(pricing_tier="BUDGET", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.searchQueryOverride == "existing override"
        assert product.pricingTier == "BUDGET"


def test_apply_sets_dynamic_pricing_configured_at_on_first_configure(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.dynamicPricingConfiguredAt is None  # never configured yet
        apply_pane_config(s, product, PaneConfig(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.dynamicPricingConfiguredAt is not None


def test_apply_does_not_overwrite_existing_configured_at(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        first_timestamp = product.dynamicPricingConfiguredAt

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(min_price_override=500))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.dynamicPricingConfiguredAt == first_timestamp


def test_apply_raises_missing_fields_when_never_configured_and_no_tier(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        with pytest.raises(PaneConfigError) as exc_info:
            apply_pane_config(s, product, PaneConfig(frequency_unit="hour", frequency_interval=6))
        assert exc_info.value.missing_fields == ["pricing tier (BUDGET, COMPETITIVE, or PREMIUM)"]

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.dynamicPricingEnabled is False  # no partial write


def test_apply_falls_back_to_shop_default_tier_when_never_configured(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        shop = product.shopDomain
        s.add(models.ShopSettings(shopDomain=shop, defaultPricingTier="BUDGET"))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.pricingTier == "BUDGET"

    with get_db() as s:
        s.query(models.ShopSettings).filter(models.ShopSettings.shopDomain == shop).delete()


def test_apply_ignores_discovery_and_listing_cap_after_first_configure(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(
            pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6,
            discovery_num_results=20, listing_expansion_cap=10,
        ))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.discoveryNumResults == 20
        assert product.listingExpansionCap == 10

    # Second call (product already configured) tries to change both — must be ignored.
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(discovery_num_results=5, listing_expansion_cap=2))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.discoveryNumResults == 20  # unchanged
        assert product.listingExpansionCap == 10   # unchanged
