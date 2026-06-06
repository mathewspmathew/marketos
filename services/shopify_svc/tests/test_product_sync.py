from unittest.mock import patch

from fastapi.testclient import TestClient

from services.api_gateway.main import app

client = TestClient(app)


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
