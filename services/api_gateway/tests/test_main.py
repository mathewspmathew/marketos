import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.api_gateway.main import app
from services.common.db import get_db
from services.common import models

client = TestClient(app)


class _FakeSession:
    def __init__(self):
        self.executed = []
        self.commits = 0

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return MagicMock(rowcount=3)

    def commit(self):
        self.commits += 1


@contextmanager
def _fake_get_db(session):
    yield session


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_product_updated_queues_semantics():
    with patch("services.api_gateway.main.get_db", lambda: _fake_get_db(_FakeSession())), \
         patch("services.api_gateway.main.claim_and_enqueue_semantics") as claim:
        resp = client.post("/internal/shopify/product-updated?product_id=gid://shopify/Product/1")
    assert resp.status_code == 200
    assert resp.json() == {"queued": True, "product_id": "gid://shopify/Product/1"}
    claim.assert_called_once()
    assert claim.call_args.kwargs["ids"] == ["gid://shopify/Product/1"]


def test_product_updated_requires_product_id():
    resp = client.post("/internal/shopify/product-updated?product_id=")
    assert resp.status_code == 422


def test_product_updated_failure_logged_and_500():
    with patch("services.api_gateway.main.get_db", lambda: _fake_get_db(_FakeSession())), \
         patch("services.api_gateway.main.claim_and_enqueue_semantics",
               side_effect=RuntimeError("db down")), \
         patch("services.api_gateway.main.logger") as log:
        resp = client.post("/internal/shopify/product-updated?product_id=gid://shopify/Product/1")
    assert resp.status_code == 500
    log.exception.assert_called_once()
    assert log.exception.call_args.args[0] == "shopify_product_updated_failed"


def test_retry_failed_semantics_resets_rows():
    session = _FakeSession()
    with patch("services.api_gateway.main.get_db", lambda: _fake_get_db(session)):
        resp = client.post("/internal/shopify/retry-failed-semantics?shop_domain=demo.myshopify.com")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "reset": 3}
    assert session.commits == 1


def test_retry_failed_semantics_requires_shop_domain():
    resp = client.post("/internal/shopify/retry-failed-semantics?shop_domain=")
    assert resp.status_code == 422


def test_retry_failed_semantics_failure_logged_and_500():
    class _BoomSession(_FakeSession):
        def execute(self, stmt, params=None):
            raise RuntimeError("db down")

    with patch("services.api_gateway.main.get_db", lambda: _fake_get_db(_BoomSession())), \
         patch("services.api_gateway.main.logger") as log:
        resp = client.post("/internal/shopify/retry-failed-semantics?shop_domain=demo.myshopify.com")
    assert resp.status_code == 500
    log.exception.assert_called_once()
    assert log.exception.call_args.args[0] == "retry_failed_semantics_failed"


def test_backfill_semantics_queues_claimed():
    with patch("services.api_gateway.main.get_db", lambda: _fake_get_db(_FakeSession())), \
         patch("services.api_gateway.main.claim_and_enqueue_semantics",
               return_value=["p1", "p2"]):
        resp = client.post("/internal/shopify/backfill-semantics")
    assert resp.status_code == 200
    assert resp.json() == {"queued": 2, "product_ids": ["p1", "p2"]}


def test_backfill_semantics_failure_logged_and_500():
    with patch("services.api_gateway.main.get_db", lambda: _fake_get_db(_FakeSession())), \
         patch("services.api_gateway.main.claim_and_enqueue_semantics",
               side_effect=RuntimeError("db down")), \
         patch("services.api_gateway.main.logger") as log:
        resp = client.post("/internal/shopify/backfill-semantics")
    assert resp.status_code == 500
    log.exception.assert_called_once()
    assert log.exception.call_args.args[0] == "backfill_shopify_semantics_failed"


@pytest.fixture
def sync_shop():
    shop = f"sync-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
    yield shop
    with get_db() as s:
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_sync_products_claims_and_enqueues_task(sync_shop):
    with patch("services.api_gateway.main.celery_app.send_task") as send:
        resp = client.post(f"/internal/shopify/sync-products?shop_domain={sync_shop}")
    assert resp.status_code == 200
    assert resp.json() == {"queued": True, "shop_domain": sync_shop}
    send.assert_called_once_with(
        "shopify_sync.pull_products",
        args=[sync_shop],
        queue="shopify_sync_queue",
    )
    with get_db() as s:
        row = s.get(models.ShopifyUser, sync_shop)
        assert row.productSyncState == "SYNCING"


def test_sync_products_no_ops_when_already_syncing_recently(sync_shop):
    with get_db() as s:
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == sync_shop).update(
            {"productSyncState": "SYNCING", "productSyncStartedAt": datetime.now(timezone.utc)}
        )
    with patch("services.api_gateway.main.celery_app.send_task") as send:
        resp = client.post(f"/internal/shopify/sync-products?shop_domain={sync_shop}")
    assert resp.status_code == 200
    assert resp.json() == {"queued": False, "reason": "already_syncing", "shop_domain": sync_shop}
    send.assert_not_called()


def test_sync_products_reclaims_after_stuck_10_minutes(sync_shop):
    with get_db() as s:
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == sync_shop).update(
            {"productSyncState": "SYNCING", "productSyncStartedAt": datetime.now(timezone.utc) - timedelta(minutes=11)}
        )
    with patch("services.api_gateway.main.celery_app.send_task") as send:
        resp = client.post(f"/internal/shopify/sync-products?shop_domain={sync_shop}")
    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    send.assert_called_once()


def test_sync_products_retry_button_ignores_error_state(sync_shop):
    with get_db() as s:
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == sync_shop).update(
            {"productSyncState": "ERROR"}
        )
    with patch("services.api_gateway.main.celery_app.send_task") as send:
        resp = client.post(f"/internal/shopify/sync-products?shop_domain={sync_shop}")
    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    send.assert_called_once()


def test_sync_products_auto_kick_skips_error_state(sync_shop):
    with get_db() as s:
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == sync_shop).update(
            {"productSyncState": "ERROR"}
        )
    with patch("services.api_gateway.main.celery_app.send_task") as send:
        resp = client.post(
            f"/internal/shopify/sync-products?shop_domain={sync_shop}&skip_if_error=true"
        )
    assert resp.status_code == 200
    assert resp.json() == {"queued": False, "reason": "error_awaits_manual_retry", "shop_domain": sync_shop}
    send.assert_not_called()


def test_sync_products_auto_kick_skips_already_synced_shop(sync_shop):
    with get_db() as s:
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == sync_shop).update(
            {"productSyncedAt": datetime.now(timezone.utc)}
        )
        s.add(models.ShopifyProduct(
            id=f"gid://shopify/Product/{uuid.uuid4().hex[:8]}", shopDomain=sync_shop, title="Existing",
        ))
    with patch("services.api_gateway.main.celery_app.send_task") as send:
        resp = client.post(
            f"/internal/shopify/sync-products?shop_domain={sync_shop}&only_if_never_synced=true"
        )
    assert resp.status_code == 200
    assert resp.json() == {"queued": False, "reason": "already_synced", "shop_domain": sync_shop}
    send.assert_not_called()


def test_sync_products_auto_kick_fires_for_never_synced_shop(sync_shop):
    with patch("services.api_gateway.main.celery_app.send_task") as send:
        resp = client.post(
            f"/internal/shopify/sync-products?shop_domain={sync_shop}&only_if_never_synced=true"
        )
    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    send.assert_called_once()


def test_sync_products_requires_shop_domain():
    resp = client.post("/internal/shopify/sync-products?shop_domain=")
    assert resp.status_code == 422


def test_sync_products_dispatch_failure_logged_and_503(sync_shop):
    with patch("services.api_gateway.main.celery_app.send_task",
               side_effect=RuntimeError("broker down")), \
         patch("services.api_gateway.main.logger") as log:
        resp = client.post(f"/internal/shopify/sync-products?shop_domain={sync_shop}")
    assert resp.status_code == 503
    log.exception.assert_called_once()
    assert log.exception.call_args.args[0] == "pull_products_dispatch_failed"
