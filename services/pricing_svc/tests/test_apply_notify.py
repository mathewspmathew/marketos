"""
services/pricing_svc/tests/test_apply_notify.py

Tests that _apply() fires a fire-and-forget price-change notification POST
to the JS app's internal.notify-price-change route after a successful
auto-apply, gated on ShopSettings.priceChangeNotificationsEnabled, and that
the notification call can never fail or block the apply path itself.
"""
import uuid
from unittest.mock import patch

import pytest

from services.common.db import get_db
from services.common import models
from services.pricing_svc.apply import _apply


@pytest.fixture
def seeded_apply_scenario():
    shop = f"apply-notify-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    decision_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopSettings(
            shopDomain=shop, markupPct=0.1, minCompetitorsToPrice=1, topKCompetitors=3,
            maxAutoApplyChangePct=0.5, lifetimeCapPct=0.5, budgetUndercut=0.05,
            premiumUplift=0.05, includeOosInPricing=False, minChangePctThreshold=0.001,
            minFreshnessHours=999999, currency="INR",
            notifyEmail="merchant@example.com", priceChangeNotificationsEnabled=True,
        ))
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Notify Apply Test Product",
            dynamicPricingEnabled=True,
        ))
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default", currentPrice=100.00, basePrice=100.00,
        ))
        s.flush()
        s.add(models.PriceDecision(
            id=decision_id, shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=100.00, newPrice=90.00, reason="auto price drop", autoApplied=True,
        ))

    yield shop, product_id, variant_id, decision_id

    with get_db() as s:
        s.query(models.PriceDecision).filter(models.PriceDecision.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopSettings).filter(models.ShopSettings.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


_FAKE_SUCCESS = {
    "data": {"productVariantsBulkUpdate": {
        "productVariants": [{"id": "x", "price": "90.00"}],
        "userErrors": [],
    }}
}


def test_apply_posts_notification_when_enabled(seeded_apply_scenario, monkeypatch):
    shop, product_id, _, decision_id = seeded_apply_scenario
    monkeypatch.setenv("APP_URL", "http://localhost:3000")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")

    with patch("services.pricing_svc.apply.call_shopify_admin", return_value=_FAKE_SUCCESS), \
         patch("services.pricing_svc.apply.httpx.post") as mock_post:
        result = _apply(shop, product_id, decision_id)

    assert result["ok"] is True
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    payload = kwargs["json"]
    assert payload["shopDomain"] == shop
    assert payload["productTitle"] == "Notify Apply Test Product"
    assert payload["currency"] == "INR"
    assert payload["variants"] == [{"variantTitle": "Default", "oldPrice": "100.00", "newPrice": "90.00"}]


def test_apply_skips_notification_when_disabled(seeded_apply_scenario, monkeypatch):
    shop, product_id, _, decision_id = seeded_apply_scenario
    monkeypatch.setenv("APP_URL", "http://localhost:3000")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
    with get_db() as s:
        s.query(models.ShopSettings).filter(models.ShopSettings.shopDomain == shop).update(
            {"priceChangeNotificationsEnabled": False}
        )

    with patch("services.pricing_svc.apply.call_shopify_admin", return_value=_FAKE_SUCCESS), \
         patch("services.pricing_svc.apply.httpx.post") as mock_post:
        result = _apply(shop, product_id, decision_id)

    assert result["ok"] is True
    mock_post.assert_not_called()


def test_apply_succeeds_even_if_notification_post_raises(seeded_apply_scenario, monkeypatch):
    shop, product_id, _, decision_id = seeded_apply_scenario
    monkeypatch.setenv("APP_URL", "http://localhost:3000")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")

    with patch("services.pricing_svc.apply.call_shopify_admin", return_value=_FAKE_SUCCESS), \
         patch("services.pricing_svc.apply.httpx.post", side_effect=Exception("network down")):
        result = _apply(shop, product_id, decision_id)

    assert result["ok"] is True
    assert result["applied"] == 1
