from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app
from services.common.db import get_db
from services.common.models import ShopifyUser
from services.conftest import INTERNAL_TOKEN_HEADERS

client = TestClient(app, headers=INTERNAL_TOKEN_HEADERS)


@pytest.fixture
def shopify_user_row():
    """The atomic sync-slot claim (api_gateway/main.py's _CLAIM_SYNC_SLOT_SQL,
    added in 8208292) is a plain UPDATE — it matches zero rows, and the
    endpoint reports "already_syncing", unless a ShopifyUser row already
    exists for the shop. Only shopDomain is required (everything else
    defaults, per services/common/models.py's ShopifyUser)."""
    with get_db() as s:
        s.add(ShopifyUser(shopDomain="demo.myshopify.com"))
    yield
    with get_db() as s:
        s.query(ShopifyUser).filter(ShopifyUser.shopDomain == "demo.myshopify.com").delete()


def test_sync_products_enqueues_task(shopify_user_row):
    with patch("services.api_gateway.main.celery_app.send_task") as send:
        resp = client.post("/internal/shopify/sync-products?shop_domain=demo.myshopify.com")
    assert resp.status_code == 200
    assert resp.json() == {"queued": True, "shop_domain": "demo.myshopify.com"}
    send.assert_called_once_with(
        "shopify_sync.pull_products",
        args=["demo.myshopify.com"],
        queue="shopify_sync_queue",
    )


def test_sync_products_requires_shop_domain():
    resp = client.post("/internal/shopify/sync-products?shop_domain=")
    assert resp.status_code == 422


from services.shopify_svc.main import (
    _map_product_node,
    _map_variant_node,
    _diff_new_ids,
)


def test_map_product_node_full():
    node = {
        "id": "gid://shopify/Product/1",
        "title": "Classmate Notebook",
        "descriptionHtml": "<p>nice</p>",
        "vendor": "Classmate",
        "productType": "Notebooks",
        "handle": "classmate-notebook",
        "status": "ACTIVE",
        "tags": ["stationery", "school"],
        "featuredImage": {"url": "https://img/1.jpg"},
    }
    assert _map_product_node(node, "demo.myshopify.com") == {
        "id": "gid://shopify/Product/1",
        "shopDomain": "demo.myshopify.com",
        "title": "Classmate Notebook",
        "description": "<p>nice</p>",
        "vendor": "Classmate",
        "productType": "Notebooks",
        "tags": ["stationery", "school"],
        "imageUrl": "https://img/1.jpg",
        "handle": "classmate-notebook",
        "status": "ACTIVE",
    }


def test_map_product_node_handles_nulls():
    node = {"id": "gid://shopify/Product/2", "title": "Bare"}
    out = _map_product_node(node, "demo.myshopify.com")
    assert out["description"] == ""
    assert out["productType"] == ""
    assert out["tags"] == []
    assert out["imageUrl"] is None
    assert out["status"] == "ACTIVE"


def test_map_variant_node():
    vnode = {
        "id": "gid://shopify/ProductVariant/10",
        "title": "Default",
        "price": "145.00",
        "compareAtPrice": "180.00",
        "sku": "CM-NB",
        "barcode": None,
        "image": {"url": "https://img/v.jpg"},
        "selectedOptions": [{"name": "Size", "value": "Short"}],
    }
    assert _map_variant_node(vnode, "gid://shopify/Product/1") == {
        "id": "gid://shopify/ProductVariant/10",
        "productId": "gid://shopify/Product/1",
        "title": "Default",
        "currentPrice": "145.00",
        "compareAtPrice": "180.00",
        "sku": "CM-NB",
        "barcode": None,
        "imageUrl": "https://img/v.jpg",
        "options": {"Size": "Short"},
        "basePrice": "145.00",
    }


def test_map_variant_node_defaults_title_and_price():
    vnode = {"id": "gid://shopify/ProductVariant/11"}
    out = _map_variant_node(vnode, "gid://shopify/Product/1")
    assert out["title"] == "Default Title"
    assert out["currentPrice"] == "0"
    assert out["options"] == {}
    # No price from Shopify → no anchor; basePrice stays NULL, never 0.
    assert out["basePrice"] is None


def test_diff_new_ids():
    assert _diff_new_ids({"a", "b"}, ["a", "b", "c"]) == ["c"]
    assert _diff_new_ids(set(), ["x"]) == ["x"]
    assert _diff_new_ids({"a"}, ["a"]) == []


from contextlib import contextmanager
from unittest.mock import MagicMock

import services.shopify_svc.main as sync_mod


class _FakeSession:
    """Records execute() calls; returns canned existing-ids for the SELECT."""
    def __init__(self, existing_ids):
        self._existing = existing_ids
        self.executed = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((sql, params))
        if 'SELECT id FROM "ShopifyProduct"' in sql:
            return [(i,) for i in self._existing]
        return MagicMock()


@contextmanager
def _fake_get_db(session):
    yield session


_FAKE_NODES = [
    {
        "id": "gid://shopify/Product/1", "title": "P1", "descriptionHtml": "",
        "vendor": "V", "productType": "T", "handle": "p1", "status": "ACTIVE",
        "tags": ["a"], "featuredImage": {"url": "u1"},
        "variants": {"edges": [{"node": {
            "id": "gid://shopify/ProductVariant/10", "title": "d",
            "price": "10.00", "compareAtPrice": None, "sku": "s1",
            "barcode": None, "image": None, "selectedOptions": [],
        }}]},
    },
]


def test_pull_products_happy_path(monkeypatch):
    session = _FakeSession(existing_ids=set())  # product 1 is NEW
    monkeypatch.setattr(sync_mod, "_get_offline_token", lambda s: "tok")
    monkeypatch.setattr(sync_mod, "_fetch_products", lambda s, t: _FAKE_NODES)
    monkeypatch.setattr(sync_mod, "get_db", lambda: _fake_get_db(session))
    send = MagicMock()
    monkeypatch.setattr(sync_mod.app, "send_task", send)

    claim = MagicMock(return_value=["gid://shopify/Product/1"])
    monkeypatch.setattr(sync_mod, "claim_and_enqueue_semantics", claim)

    result = sync_mod.pull_products("demo.myshopify.com")

    assert result == {"ok": True, "count": 1, "new": 1}
    claim.assert_called_once()
    assert claim.call_args.kwargs["ids"] == ["gid://shopify/Product/1"]
    assert any("productSyncState" in sql and "SYNCED" in str(p)
               for sql, p in session.executed)


def test_pull_products_no_token(monkeypatch):
    session = _FakeSession(existing_ids=set())
    monkeypatch.setattr(sync_mod, "_get_offline_token", lambda s: None)
    monkeypatch.setattr(sync_mod, "get_db", lambda: _fake_get_db(session))
    send = MagicMock()
    monkeypatch.setattr(sync_mod.app, "send_task", send)

    result = sync_mod.pull_products("demo.myshopify.com")

    assert result == {"ok": False, "reason": "no_offline_token"}
    send.assert_not_called()
    assert any("ERROR" in str(p) for _, p in session.executed)


def test_pull_products_error_sets_state_and_raises(monkeypatch):
    session = _FakeSession(existing_ids=set())
    monkeypatch.setattr(sync_mod, "_get_offline_token", lambda s: "tok")
    def boom(s, t): raise RuntimeError("shopify down")
    monkeypatch.setattr(sync_mod, "_fetch_products", boom)
    monkeypatch.setattr(sync_mod, "get_db", lambda: _fake_get_db(session))

    import pytest
    with pytest.raises(RuntimeError):
        sync_mod.pull_products("demo.myshopify.com")
    assert any("ERROR" in str(p) for _, p in session.executed)


def test_product_update_webhook_enqueues_task():
    with patch("services.api_gateway.main.celery_app.send_task") as send:
        resp = client.post("/internal/shopify/product-update-webhook", json={
            "shop_domain": "demo.myshopify.com",
            "payload": {"id": 123, "title": "Test Product"},
        })
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    send.assert_called_once_with(
        "shopify_sync.handle_product_update",
        args=["demo.myshopify.com", {"id": 123, "title": "Test Product"}],
        queue="shopify_sync_queue",
    )


def test_product_update_webhook_dispatch_failure_returns_ok_false():
    with patch("services.api_gateway.main.celery_app.send_task", side_effect=RuntimeError("broker down")):
        resp = client.post("/internal/shopify/product-update-webhook", json={
            "shop_domain": "demo.myshopify.com",
            "payload": {"id": 123},
        })
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "error": "failed to queue product update"}
