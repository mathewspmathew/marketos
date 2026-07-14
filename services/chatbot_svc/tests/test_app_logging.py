import os
os.environ.setdefault("GROQ_API_KEY", "test")

import structlog

from services.chatbot_svc.app import app  # noqa: F401 — import triggers setup_logging()


def test_setup_logging_called_at_import_configures_structlog():
    """app.py must call setup_logging() at import time so every log line
    emitted by the service (including deep inside apply_config.py) is
    JSON-on-stdout, matching every Celery worker's behavior."""
    assert structlog.is_configured()


def test_bind_request_context_sets_and_clears_contextvars():
    from services.chatbot_svc.app import _bind_request_context, _clear_request_context

    assert structlog.contextvars.get_contextvars() == {}
    _bind_request_context(shop_domain="shop1.myshopify.com", session_id="sess-abc")
    bound = structlog.contextvars.get_contextvars()
    assert bound["shop_domain"] == "shop1.myshopify.com"
    assert bound["session_id"] == "sess-abc"

    _clear_request_context()
    assert structlog.contextvars.get_contextvars() == {}


import json

import pytest
from fastapi.testclient import TestClient

from services.common import logging_config
from services.chatbot_svc import app as app_module


def test_chat_endpoint_logs_exception_before_responding(monkeypatch, capsys):
    # app.py's setup_logging() ran once at MODULE IMPORT time, before this
    # test's capsys ever patched sys.stdout — the StreamHandler it created
    # is bound to the real stdout stream, not this test's capture buffer.
    # Re-run setup_logging() here (same shared function, not a second
    # config) so the handler rebinds to capsys's current stream and this
    # test's capsys.readouterr() actually captures the log line. (Verified
    # empirically: without this rebind, capsys.readouterr().out is always
    # empty even though the JSON line does print to the real terminal.)
    logging_config.setup_logging()

    async def _boom(*args, **kwargs):
        raise ValueError("simulated agent failure")

    monkeypatch.setattr(app_module.agent, "run", _boom)
    monkeypatch.setattr(app_module, "_ensure_session", lambda *a, **k: "sess-test-123")
    monkeypatch.setattr(app_module, "build_context", lambda *a, **k: [])
    monkeypatch.setattr(app_module, "_record", lambda *a, **k: None)

    client = TestClient(app_module.app)
    with client.stream(
        "POST", "/chat",
        json={"shop_domain": "shop1.myshopify.com", "message": "hello"},
    ) as resp:
        events = [line for line in resp.iter_lines() if line]

    assert any("simulated agent failure" in line for line in events)

    out = capsys.readouterr().out.strip().splitlines()
    matches = [json.loads(l) for l in out if json.loads(l).get("event") == "chat_request_failed"]
    assert len(matches) == 1
    assert matches[0]["level"] == "error"
    assert "simulated agent failure" in str(matches[0].get("exception", "")) or matches[0].get("exc_info")


def test_query_studio_unexpected_exception_returns_500_and_logs(monkeypatch, capsys):
    # Same rebind reason as the test above — setup_logging() must be called
    # again inside this test body so its StreamHandler binds to THIS test's
    # capsys-patched stdout, not the real stdout bound at module-import time.
    logging_config.setup_logging()

    def _boom(*args, **kwargs):
        raise ValueError("simulated query studio failure")

    monkeypatch.setattr(app_module.t_query_studio, "propose_queries", _boom)

    client = TestClient(app_module.app)
    resp = client.post(
        "/query-studio",
        json={"shop_domain": "shop1.myshopify.com", "product_id": "p1", "focus": "", "mode": "propose"},
    )
    assert resp.status_code == 500

    out = capsys.readouterr().out.strip().splitlines()
    matches = [json.loads(l) for l in out if json.loads(l).get("event") == "query_studio_request_failed"]
    assert len(matches) == 1
    assert matches[0]["level"] == "error"
