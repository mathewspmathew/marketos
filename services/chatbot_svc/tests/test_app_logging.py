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
