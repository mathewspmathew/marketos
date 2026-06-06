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
    }


def test_map_variant_node_defaults_title_and_price():
    vnode = {"id": "gid://shopify/ProductVariant/11"}
    out = _map_variant_node(vnode, "gid://shopify/Product/1")
    assert out["title"] == "Default Title"
    assert out["currentPrice"] == "0"
    assert out["options"] == {}


def test_diff_new_ids():
    assert _diff_new_ids({"a", "b"}, ["a", "b", "c"]) == ["c"]
    assert _diff_new_ids(set(), ["x"]) == ["x"]
    assert _diff_new_ids({"a"}, ["a"]) == []
