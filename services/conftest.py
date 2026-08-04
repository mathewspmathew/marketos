"""services/conftest.py

Repo-wide pytest safety net: block every test from dispatching a real Celery
task by default. Without this, a test that forgets to mock `send_task` sends
the task to whatever REDIS_URL/DATABASE_URL is configured — locally, the same
broker/DB docker-compose's real workers consume from — so a live worker can
pick up the task and race the test's fixture teardown (this is what caused
the VariantCompetitorStats FK violation this fixture exists to prevent).

Tests that need to assert on dispatch behavior (e.g. test_stats_dispatch_guard.py)
apply their own monkeypatch.setattr(<module>.app, "send_task", ...) on top of
this — that overrides cleanly, no conflict.

Also the shared X-Internal-Token header for any test that hits api_gateway's
FastAPI app via TestClient — every /internal/* route 403s without it
(services/api_gateway/main.py's require_internal_token middleware).
"""
import os
from unittest.mock import MagicMock

import pytest

from services.common.celery_app import app

INTERNAL_TOKEN_HEADERS = {"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]}


@pytest.fixture(autouse=True)
def _block_real_task_dispatch(monkeypatch):
    monkeypatch.setattr(app, "send_task", MagicMock())
