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
from services.common.pane_config import PaneConfig, PaneConfigError, apply_pane_config, resume_dynamic_pricing


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


def test_apply_does_not_rearm_when_frequency_unchanged(seeded_product_with_url):
    """Regression test: the pane resends frequency on every save, even ones
    that only change bounds/search-query/etc. A second save with the exact
    same frequency must not reset the rescrape countdown."""
    product_id, url_id = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(pricing_tier="COMPETITIVE", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        url = s.get(models.ProductUrl, url_id)
        first_next_run = url.nextRunAt

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        result = apply_pane_config(s, product, PaneConfig(
            pricing_tier="COMPETITIVE", frequency_unit="hour", frequency_interval=6,
            min_price_override=500, max_price_override=900,
        ))
        assert result["rearmedCount"] == 0

    with get_db() as s:
        url = s.get(models.ProductUrl, url_id)
        assert url.nextRunAt == first_next_run


def test_apply_rearms_when_frequency_changed(seeded_product_with_url):
    """A genuine frequency change (not just a resend of the same value)
    must still re-arm the schedule."""
    product_id, url_id = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(pricing_tier="COMPETITIVE", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        result = apply_pane_config(s, product, PaneConfig(
            pricing_tier="COMPETITIVE", frequency_unit="day", frequency_interval=1,
        ))
        assert result["rearmedCount"] == 1

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.frequencyUnit == "day"
        assert product.frequencyInterval == 1


def test_apply_invalid_bounds_writes_nothing(seeded_product_with_url):
    product_id, url_id = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        with pytest.raises(PaneConfigError):
            apply_pane_config(s, product, PaneConfig(
                pricing_tier="PREMIUM",
                frequency_unit="hour",
                frequency_interval=6,
                min_price_override=100,
                max_price_override=50,
            ))

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


def test_apply_queues_discovery_on_first_enable_with_query(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        product.searchQuery = "wireless earbuds"
        s.flush()
        apply_pane_config(s, product, PaneConfig(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        jobs = s.query(models.DiscoveryJob).filter(models.DiscoveryJob.shopifyProductId == product_id).all()
        assert len(jobs) == 1
        assert jobs[0].query == "wireless earbuds"
        assert jobs[0].status == "QUEUED"
        s.query(models.DiscoveryJob).filter(models.DiscoveryJob.shopifyProductId == product_id).delete()


def test_apply_does_not_queue_discovery_without_a_query(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        jobs = s.query(models.DiscoveryJob).filter(models.DiscoveryJob.shopifyProductId == product_id).all()
        assert jobs == []


def test_apply_does_not_requeue_discovery_on_second_configure(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        product.searchQuery = "wireless earbuds"
        s.flush()
        apply_pane_config(s, product, PaneConfig(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(min_price_override=500))

    with get_db() as s:
        jobs = s.query(models.DiscoveryJob).filter(models.DiscoveryJob.shopifyProductId == product_id).all()
        assert len(jobs) == 1  # not duplicated
        s.query(models.DiscoveryJob).filter(models.DiscoveryJob.shopifyProductId == product_id).delete()


def test_apply_snapshots_base_price_on_first_enable(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    variant_id = str(uuid.uuid4())
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default",
            currentPrice=49.99, basePrice=None,
        ))
        s.flush()
        apply_pane_config(s, product, PaneConfig(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        variant = s.get(models.ShopifyVariant, variant_id)
        product = s.get(models.ShopifyProduct, product_id)
        assert float(variant.basePrice) == 49.99
        assert float(product.avgBasePrice) == 49.99
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete()


def test_resume_flips_flag_and_rearms_stale_urls(seeded_product_with_url):
    product_id, url_id = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        product.dynamicPricingEnabled = False
        product.frequencyUnit = "hour"
        product.frequencyInterval = 6

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        result = resume_dynamic_pricing(s, product)
        assert result["dynamicPricingEnabled"] == {"old": False, "new": True}
        assert result["rearmedCount"] == 1

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        url = s.get(models.ProductUrl, url_id)
        assert product.dynamicPricingEnabled is True
        assert url.nextRunAt > datetime.now(timezone.utc).replace(tzinfo=None)


def test_resume_requires_no_config_at_all(seeded_product_with_url):
    """Bare resume works even with no tier/frequency ever set — no gate,
    unlike apply_pane_config. Mirrors app.products.jsx's resumeDynamic."""
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.pricingTier == "COMPETITIVE"  # DB default, never explicitly set
        result = resume_dynamic_pricing(s, product)
        assert result["dynamicPricingEnabled"]["new"] is True


def test_apply_does_not_overwrite_existing_base_price(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    variant_id = str(uuid.uuid4())
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default",
            currentPrice=49.99, basePrice=39.99,
        ))
        s.flush()
        apply_pane_config(s, product, PaneConfig(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6))

    with get_db() as s:
        variant = s.get(models.ShopifyVariant, variant_id)
        assert float(variant.basePrice) == 39.99  # unchanged, first-seen anchor preserved
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete()


def test_apply_clear_min_price_override_writes_null(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(
            pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6,
            min_price_override=500,
        ))
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert float(product.minPriceOverride) == 500.0

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(clear_min_price_override=True))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.minPriceOverride is None


def test_apply_clear_max_price_override_writes_null(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(
            pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6,
            max_price_override=1200,
        ))
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert float(product.maxPriceOverride) == 1200.0

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(clear_max_price_override=True))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.maxPriceOverride is None


def test_apply_clear_flag_ignores_simultaneous_value(seeded_product_with_url):
    """If both a value and its clear flag are sent, clear wins — this should
    never happen from real callers, but the contract must be unambiguous."""
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        apply_pane_config(s, product, PaneConfig(
            pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6,
            min_price_override=500, clear_min_price_override=True,
        ))
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.minPriceOverride is None


def test_last_discovery_num_results_round_trips(seeded_product_with_url):
    product_id, _ = seeded_product_with_url
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        product.lastDiscoveryNumResults = 15

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert product.lastDiscoveryNumResults == 15
