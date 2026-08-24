import uuid

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app
from services.common.db import get_db
from services.common import models
from services.conftest import INTERNAL_TOKEN_HEADERS

_client = TestClient(app, headers=INTERNAL_TOKEN_HEADERS)


@pytest.fixture
def seeded_product():
    shop = f"http-dp-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="HTTP Test Product",
            dynamicPricingEnabled=False,
        ))

    yield shop, product_id

    with get_db() as s:
        s.query(models.DiscoveryJob).filter(models.DiscoveryJob.shopifyProductId == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def _blank_config(**overrides):
    defaults = dict(
        search_query_override=None, pricing_tier=None, min_price_override=None,
        max_price_override=None, frequency_unit=None, frequency_interval=None,
        discovery_num_results=None, listing_expansion_cap=None,
        clear_min_price_override=False, clear_max_price_override=False,
    )
    defaults.update(overrides)
    return defaults


def test_skip_reasons_endpoint_returns_full_taxonomy():
    resp = _client.get("/internal/dynamic-pricing/skip-reasons")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    reasons = body["reasons"]
    assert set(reasons) == {
        "below_min_competitors", "no_change", "clamped_per_round", "clamped_lifetime_cap",
        "auto_update_off",
    }
    assert reasons["below_min_competitors"]["blocked"] is True
    assert reasons["clamped_per_round"]["blocked"] is False
    assert reasons["auto_update_off"]["blocked"] is False
    for entry in reasons.values():
        assert "label" in entry


def test_apply_endpoint_success(seeded_product):
    shop, pid = seeded_product
    r = _client.post("/internal/dynamic-pricing/apply", json={
        "shop_domain": shop, "product_id": pid,
        "config": _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    with get_db() as s:
        product = s.get(models.ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is True
        assert product.pricingTier == "PREMIUM"


def test_apply_endpoint_missing_fields_returns_ok_false(seeded_product):
    shop, pid = seeded_product
    r = _client.post("/internal/dynamic-pricing/apply", json={
        "shop_domain": shop, "product_id": pid, "config": _blank_config(),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "pricing tier" in body["error"]


def test_apply_endpoint_unknown_product_returns_ok_false(seeded_product):
    shop, _ = seeded_product
    r = _client.post("/internal/dynamic-pricing/apply", json={
        "shop_domain": shop, "product_id": "nonexistent", "config": _blank_config(),
    })
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_apply_endpoint_clear_min_price_override(seeded_product):
    shop, pid = seeded_product
    r = _client.post("/internal/dynamic-pricing/apply", json={
        "shop_domain": shop, "product_id": pid,
        "config": _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6, min_price_override=500),
    })
    assert r.json()["ok"] is True
    with get_db() as s:
        assert float(s.get(models.ShopifyProduct, pid).minPriceOverride) == 500.0

    r = _client.post("/internal/dynamic-pricing/apply", json={
        "shop_domain": shop, "product_id": pid,
        "config": _blank_config(clear_min_price_override=True),
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    with get_db() as s:
        assert s.get(models.ShopifyProduct, pid).minPriceOverride is None


def test_pause_then_resume_endpoints(seeded_product):
    shop, pid = seeded_product
    _client.post("/internal/dynamic-pricing/apply", json={
        "shop_domain": shop, "product_id": pid,
        "config": _blank_config(pricing_tier="BUDGET", frequency_unit="day", frequency_interval=1),
    })

    r = _client.post("/internal/dynamic-pricing/pause", json={"shop_domain": shop, "product_id": pid})
    assert r.json()["ok"] is True
    with get_db() as s:
        assert s.get(models.ShopifyProduct, pid).dynamicPricingEnabled is False

    r = _client.post("/internal/dynamic-pricing/resume", json={"shop_domain": shop, "product_id": pid})
    assert r.json()["ok"] is True
    with get_db() as s:
        assert s.get(models.ShopifyProduct, pid).dynamicPricingEnabled is True


def test_delete_preview_endpoint(seeded_product):
    shop, pid = seeded_product
    r = _client.get("/internal/dynamic-pricing/delete-preview", params={"shop_domain": shop, "product_id": pid})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_delete_preview_endpoint_rejects_cross_shop_product(seeded_product, seed_other_shop):
    shop, _ = seeded_product
    other_shop = seed_other_shop
    r = _client.get(
        "/internal/dynamic-pricing/delete-preview",
        params={"shop_domain": shop, "product_id": "other-shop-product"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_delete_endpoint_requires_confirmation(seeded_product):
    shop, pid = seeded_product
    r = _client.post("/internal/dynamic-pricing/delete", json={"shop_domain": shop, "product_id": pid, "confirmed": False})
    assert r.json()["ok"] is False

    r = _client.post("/internal/dynamic-pricing/delete", json={"shop_domain": shop, "product_id": pid, "confirmed": True})
    assert r.json()["ok"] is True
    with get_db() as s:
        product = s.get(models.ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False


def test_rearm_shop_rearms_stale_urls_for_eligible_products_only():
    from datetime import datetime, timedelta, timezone

    shop = f"rearm-shop-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    eligible_pid = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    ineligible_pid = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    scraped_id = str(uuid.uuid4())
    stale_url_id = str(uuid.uuid4())
    healthy_url_id = str(uuid.uuid4())
    ineligible_url_id = str(uuid.uuid4())

    stale = datetime.now(timezone.utc) - timedelta(days=1)
    healthy = datetime.now(timezone.utc) + timedelta(days=1)

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=eligible_pid, shopDomain=shop, title="Eligible",
            dynamicPricingEnabled=True, frequencyUnit="hour", frequencyInterval=6,
        ))
        s.add(models.ShopifyProduct(
            id=ineligible_pid, shopDomain=shop, title="Ineligible (DP off)",
            dynamicPricingEnabled=False,
        ))
        s.add(models.ScrapedProduct(id=scraped_id, shopDomain=shop, domain="example.com", title="Competitor"))
        s.flush()
        s.add(models.ProductUrl(
            id=stale_url_id, shopDomain=shop, shopifyProductId=eligible_pid, prodId=scraped_id,
            url=f"https://example.com/{stale_url_id}", status="ACTIVE", nextRunAt=stale,
        ))
        s.add(models.ProductUrl(
            id=healthy_url_id, shopDomain=shop, shopifyProductId=eligible_pid, prodId=scraped_id,
            url=f"https://example.com/{healthy_url_id}", status="ACTIVE", nextRunAt=healthy,
        ))
        s.add(models.ProductUrl(
            id=ineligible_url_id, shopDomain=shop, shopifyProductId=ineligible_pid, prodId=scraped_id,
            url=f"https://example.com/{ineligible_url_id}", status="ACTIVE", nextRunAt=stale,
        ))

    try:
        r = _client.post(
            "/internal/dynamic-pricing/rearm-shop",
            params={"shop_domain": shop, "frequency_interval": 6, "frequency_unit": "hour"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["rearmedCount"] == 1  # only the stale URL on the eligible product

        with get_db() as s:
            stale_url = s.get(models.ProductUrl, stale_url_id)
            healthy_url = s.get(models.ProductUrl, healthy_url_id)
            ineligible_url = s.get(models.ProductUrl, ineligible_url_id)
            assert stale_url.nextRunAt > datetime.now(timezone.utc)
            # "untouched" checked within a tolerance — Postgres round-trips
            # timestamps at slightly different sub-millisecond precision.
            assert abs((healthy_url.nextRunAt - healthy).total_seconds()) < 1
            assert abs((ineligible_url.nextRunAt - stale).total_seconds()) < 1
    finally:
        with get_db() as s:
            s.query(models.ProductUrl).filter(models.ProductUrl.shopDomain == shop).delete(synchronize_session=False)
            s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)
            s.query(models.ShopifyProduct).filter(models.ShopifyProduct.shopDomain == shop).delete(synchronize_session=False)
            s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)
