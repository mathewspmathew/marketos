import uuid

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app
from services.common.db import get_db
from services.common import models

_client = TestClient(app)


@pytest.fixture
def seeded_match():
    shop = f"match-review-endpoint-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    scraped_id = str(uuid.uuid4())
    match_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(id=product_id, shopDomain=shop, title="Endpoint Test"))
        s.add(models.ScrapedProduct(id=scraped_id, shopDomain=shop, domain="comp.example.com", title="Competitor"))
        s.flush()
        s.add(models.ProductLevelMatch(
            id=match_id, shopDomain=shop,
            shopifyProductId=product_id, scrapedProductId=scraped_id,
            confidence=0.7, confidenceTier="LIKELY", reviewStatus="PENDING",
        ))

    yield shop, match_id

    with get_db() as s:
        s.query(models.ProductLevelMatch).filter(models.ProductLevelMatch.id == match_id).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_confirm_endpoint_success(seeded_match):
    shop, match_id = seeded_match
    r = _client.post("/internal/matches/review", json={"shop_domain": shop, "match_id": match_id, "action": "confirm"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["reviewStatus"] == "CONFIRMED"


def test_reject_endpoint_success(seeded_match):
    shop, match_id = seeded_match
    r = _client.post("/internal/matches/review", json={"shop_domain": shop, "match_id": match_id, "action": "reject"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["reviewStatus"] == "REJECTED"


def test_unknown_action_returns_ok_false(seeded_match):
    shop, match_id = seeded_match
    r = _client.post("/internal/matches/review", json={"shop_domain": shop, "match_id": match_id, "action": "delete"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "Unknown action" in body["error"]


def test_unknown_match_returns_ok_false(seeded_match):
    shop, match_id = seeded_match
    r = _client.post("/internal/matches/review", json={"shop_domain": shop, "match_id": "nonexistent", "action": "confirm"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
