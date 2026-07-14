"""Tests for the apply_price send_task guard in decide.py.

Before this fix, a failed dispatch to pricing.apply_price left a
PriceDecision row committed with autoApplied=True but nothing ever pushed
the price to Shopify — and worse, a retry would just hit the debounce
check (lastDecisionAt was already stamped) and silently no-op forever.

These tests drive decide_price_for_product through a full "price changed,
apply" cycle with a fake DB session, then assert that a failing send_task
is (a) logged and (b) stamped onto the decision via applyError instead of
being silently lost.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import services.pricing_svc.decide as decide_mod

_PRODUCT = SimpleNamespace(
    dynamicPricingEnabled=True, syncPrice=True, pricingTier="COMPETITIVE",
    minPriceOverride=None, maxPriceOverride=None,
    maxAutoApplyChangePctOverride=None, lifetimeCapPctOverride=None,
    lastDecisionAt=None, frequencyUnit=None, frequencyInterval=None,
)

_SETTINGS = SimpleNamespace(
    markupPct=0.1, currency="INR", minCompetitorsToPrice=1, topKCompetitors=3,
    maxAutoApplyChangePct=0.1, lifetimeCapPct=0.2, budgetUndercut=0.05,
    premiumUplift=0.05, includeOosInPricing=False,
    frequencyUnit=None, frequencyInterval=None,
    minChangePctThreshold=0.001, minFreshnessHours=24,
)

_VARIANTS = [SimpleNamespace(id="var1", currentPrice=100, basePrice=100)]

_COMP_ROWS = [SimpleNamespace(
    scraped_product_id="sp1", price=80, currency="INR", in_stock=True,
    observed_at=datetime.now(timezone.utc), match_id="m1", confidence=1.0,
    title="Comp1", domain="comp.com",
)]

_DECISION_ID = "decision123"


class _Result:
    def __init__(self, data):
        self._data = data

    def first(self):
        return self._data

    def all(self):
        return self._data


class _FakeSession:
    def __init__(self):
        self.executed = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((sql, params))
        if "SELECT" in sql and 'FROM "ShopifyProduct"' in sql:
            return _Result(_PRODUCT)
        if 'FROM "ShopSettings"' in sql:
            return _Result(_SETTINGS)
        if 'FROM "ShopifyVariant"' in sql:
            return _Result(_VARIANTS)
        if "WITH latest AS" in sql:
            return _Result(_COMP_ROWS)
        if 'INSERT INTO "PriceDecision"' in sql:
            return _Result((_DECISION_ID,))
        return _Result(None)


@contextmanager
def _fake_get_db(session):
    yield session


def _run_decide(monkeypatch, send_task):
    session = _FakeSession()
    monkeypatch.setattr(decide_mod, "get_db", lambda: _fake_get_db(session))
    fake_logger = MagicMock()
    monkeypatch.setattr(decide_mod, "logger", fake_logger)
    monkeypatch.setattr(decide_mod.app, "send_task", send_task)
    result = decide_mod.decide_price_for_product("demo.myshopify.com", "prod1")
    return result, session, fake_logger


def test_dispatch_failure_is_logged_and_stamped_on_decision(monkeypatch):
    send = MagicMock(side_effect=RuntimeError("broker down"))

    result, session, fake_logger = _run_decide(monkeypatch, send)

    # The price computation itself still succeeds and reports applied=True —
    # only the downstream dispatch failed.
    assert result["ok"] is True
    assert result["applied"] is True
    send.assert_called_once()

    fake_logger.exception.assert_called_once_with(
        "apply_price_dispatch_failed",
        shop_domain="demo.myshopify.com",
        shopify_product_id="prod1",
        decision_id=_DECISION_ID,
    )
    assert any(
        'UPDATE "PriceDecision" SET "applyError"' in sql
        and params["id"] == _DECISION_ID
        and params["e"] == "dispatch_failed"
        for sql, params in session.executed
    )


def test_dispatch_success_leaves_no_error_stamp(monkeypatch):
    send = MagicMock()

    result, session, fake_logger = _run_decide(monkeypatch, send)

    assert result["ok"] is True
    assert result["applied"] is True
    send.assert_called_once()
    fake_logger.exception.assert_not_called()
    assert not any(
        'UPDATE "PriceDecision" SET "applyError"' in sql for sql, _ in session.executed
    )
