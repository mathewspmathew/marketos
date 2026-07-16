from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from services.api_gateway.main import app

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


def test_sync_products_enqueues_task():
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


def test_sync_products_dispatch_failure_logged_and_503():
    with patch("services.api_gateway.main.celery_app.send_task",
               side_effect=RuntimeError("broker down")), \
         patch("services.api_gateway.main.logger") as log:
        resp = client.post("/internal/shopify/sync-products?shop_domain=demo.myshopify.com")
    assert resp.status_code == 503
    log.exception.assert_called_once()
    assert log.exception.call_args.args[0] == "pull_products_dispatch_failed"
