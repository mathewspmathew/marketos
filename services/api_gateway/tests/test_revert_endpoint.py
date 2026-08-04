import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app
from services.common.db import get_db
from services.common import models
from services.conftest import INTERNAL_TOKEN_HEADERS

_client = TestClient(app, headers=INTERNAL_TOKEN_HEADERS)

_FAKE_SUCCESS = {
    "data": {"productVariantsBulkUpdate": {
        "productVariants": [{"id": "x", "price": "100.00"}],
        "userErrors": [],
    }}
}


@pytest.fixture
def seeded_reverted_scenario():
    shop = f"revert-endpoint-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    decision_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(id=product_id, shopDomain=shop, title="Test", dynamicPricingEnabled=True))
        s.add(models.ShopifyVariant(id=variant_id, productId=product_id, title="Default", currentPrice=90.00))
        s.flush()
        s.add(models.PriceDecision(
            id=decision_id, shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=100.00, newPrice=90.00, reason="auto price drop",
            appliedAt=datetime.now(timezone.utc),
        ))

    yield shop, variant_id, decision_id

    with get_db() as s:
        s.query(models.PriceDecision).filter(models.PriceDecision.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_revert_endpoint_success(seeded_reverted_scenario):
    shop, variant_id, decision_id = seeded_reverted_scenario
    with patch("services.pricing_svc.revert.call_shopify_admin", return_value=_FAKE_SUCCESS):
        r = _client.post("/internal/pricing/revert", json={
            "shop_domain": shop, "variant_id": variant_id, "decision_id": decision_id,
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["oldPrice"] == 100.0
    assert body["newPrice"] == 90.0


def test_revert_endpoint_unknown_variant_returns_ok_false(seeded_reverted_scenario):
    shop, _, decision_id = seeded_reverted_scenario
    r = _client.post("/internal/pricing/revert", json={
        "shop_domain": shop, "variant_id": "nonexistent", "decision_id": decision_id,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_revert_endpoint_shopify_failure_returns_ok_false(seeded_reverted_scenario):
    shop, variant_id, decision_id = seeded_reverted_scenario
    fake_failure = {
        "data": {"productVariantsBulkUpdate": {"productVariants": [], "userErrors": [{"field": "price", "message": "bad"}]}}
    }
    with patch("services.pricing_svc.revert.call_shopify_admin", return_value=fake_failure):
        r = _client.post("/internal/pricing/revert", json={
            "shop_domain": shop, "variant_id": variant_id, "decision_id": decision_id,
        })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "bad" in body["error"]
