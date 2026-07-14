"""Tests for the per-item send_task guards in stats.py's fan-out loops.

Each fan-out loop wraps its app.send_task call in its own try/except so one
failed dispatch doesn't kill the rest of the batch or crash the task. These
tests prove that: a failing send_task is logged, not raised, and every item
in the batch still gets its own dispatch attempt.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock

import services.pricing_svc.stats as stats_mod


class _Result:
    def __init__(self, data):
        self._data = data

    def all(self):
        return self._data


class _FakeObservationSession:
    """Answers recompute_after_observation's two lookup queries."""

    def __init__(self, product_ids, variant_ids):
        self._product_ids = product_ids
        self._variant_ids = variant_ids

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if 'plm."shopifyProductId"' in sql:
            return _Result([(pid,) for pid in self._product_ids])
        if 'pm."shopifyVariantId"' in sql:
            return _Result([(vid,) for vid in self._variant_ids])
        return _Result([])


@contextmanager
def _fake_get_db(session):
    yield session


def test_recompute_for_variant_dispatch_failure_is_logged_not_raised(monkeypatch):
    monkeypatch.setattr(stats_mod, "_recompute_for_variant", lambda sd, vid: "prod1")
    fake_logger = MagicMock()
    monkeypatch.setattr(stats_mod, "logger", fake_logger)
    send = MagicMock(side_effect=RuntimeError("broker down"))
    monkeypatch.setattr(stats_mod.app, "send_task", send)

    result = stats_mod.recompute_for_variant.run("demo.myshopify.com", "var1")

    assert result == {"ok": True, "product_id": "prod1"}
    send.assert_called_once()
    fake_logger.exception.assert_called_once_with(
        "decide_for_product_dispatch_failed", shop_domain="demo.myshopify.com", product_id="prod1"
    )


def test_recompute_for_variant_dispatch_success_no_log(monkeypatch):
    monkeypatch.setattr(stats_mod, "_recompute_for_variant", lambda sd, vid: "prod1")
    fake_logger = MagicMock()
    monkeypatch.setattr(stats_mod, "logger", fake_logger)
    send = MagicMock()
    monkeypatch.setattr(stats_mod.app, "send_task", send)

    result = stats_mod.recompute_for_variant.run("demo.myshopify.com", "var1")

    assert result == {"ok": True, "product_id": "prod1"}
    send.assert_called_once()
    fake_logger.exception.assert_not_called()


def test_recompute_after_observation_continues_past_dispatch_failures(monkeypatch):
    session = _FakeObservationSession(product_ids=["p1", "p2"], variant_ids=["v1"])
    monkeypatch.setattr(stats_mod, "get_db", lambda: _fake_get_db(session))
    fake_logger = MagicMock()
    monkeypatch.setattr(stats_mod, "logger", fake_logger)
    send = MagicMock(side_effect=RuntimeError("broker down"))
    monkeypatch.setattr(stats_mod.app, "send_task", send)

    result = stats_mod.recompute_after_observation.run("demo.myshopify.com", "cv1")

    # All 3 items (2 products + 1 variant) get a dispatch attempt despite
    # every single one failing — no early exit, no unhandled exception.
    assert result == {"ok": True, "products": 2, "variants": 1}
    assert send.call_count == 3
    assert fake_logger.exception.call_count == 3
