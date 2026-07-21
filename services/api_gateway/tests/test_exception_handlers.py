"""Regression tests for main.py's global RequestValidationError/Exception
handlers — a malformed request must never bypass the {ok, error} contract
JS callers rely on (data.ok ? ... : data.error)."""
from fastapi.testclient import TestClient

from services.api_gateway.main import app

_client = TestClient(app, raise_server_exceptions=False)


def test_malformed_body_returns_ok_false_not_blank_error():
    # Missing every required field of DynamicPricingApplyRequest.
    r = _client.post("/internal/dynamic-pricing/apply", json={})
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert body["error"]


def test_wrong_type_in_body_returns_ok_false():
    r = _client.post("/internal/pricing/revert", json={
        "shop_domain": "x.myshopify.com", "variant_id": 123, "decision_id": None,
    })
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert body["error"]


def test_missing_required_query_param_returns_ok_false():
    r = _client.post("/internal/shopify/retry-failed-semantics")
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert body["error"]
