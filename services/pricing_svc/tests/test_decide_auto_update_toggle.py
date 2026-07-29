"""Tests for the shop-wide autoUpdatePriceEnabled gate in decide.py.

Turning this off must not skip calculation — only the Shopify push. Every
test here asserts the PriceDecision row still carries a real computed price
even when autoApplied ends up False.
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

_VARIANTS = [SimpleNamespace(id="var1", currentPrice=100, basePrice=100)]

_COMP_ROWS = [SimpleNamespace(
    scraped_product_id="sp1", price=98, currency="INR", in_stock=True,
    observed_at=datetime.now(timezone.utc), match_id="m1", confidence=1.0,
    title="Comp1", domain="comp.com",
)]

_DECISION_ID = "decision123"


def _settings(auto_update_price_enabled: bool):
    return SimpleNamespace(
        markupPct=0.02, currency="INR", minCompetitorsToPrice=1, topKCompetitors=3,
        maxAutoApplyChangePct=0.1, lifetimeCapPct=0.2, budgetUndercut=0.05,
        premiumUplift=0.05, includeOosInPricing=False,
        autoUpdatePriceEnabled=auto_update_price_enabled,
        frequencyUnit=None, frequencyInterval=None,
        minChangePctThreshold=0.001, minFreshnessHours=24,
    )


class _Result:
    def __init__(self, data):
        self._data = data

    def first(self):
        return self._data

    def all(self):
        return self._data


class _FakeSession:
    def __init__(self, settings):
        self.executed = []
        self.settings = settings

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((sql, params))
        if "SELECT" in sql and 'FROM "ShopifyProduct"' in sql:
            return _Result(_PRODUCT)
        if 'FROM "ShopSettings"' in sql:
            return _Result(self.settings)
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


def _run_decide(monkeypatch, auto_update_price_enabled: bool, send_task=None):
    session = _FakeSession(_settings(auto_update_price_enabled))
    monkeypatch.setattr(decide_mod, "get_db", lambda: _fake_get_db(session))
    monkeypatch.setattr(decide_mod, "logger", MagicMock())
    monkeypatch.setattr(decide_mod.app, "send_task", send_task or MagicMock())
    result = decide_mod.decide_price_for_product("demo.myshopify.com", "prod1")
    return result, session


def test_toggle_on_dispatches_as_before(monkeypatch):
    send = MagicMock()
    result, session = _run_decide(monkeypatch, auto_update_price_enabled=True, send_task=send)

    assert result["ok"] is True
    assert result["applied"] is True
    send.assert_called_once()

    insert_params = next(p for sql, p in session.executed if 'INSERT INTO "PriceDecision"' in sql)
    assert insert_params["auto"] is True
    assert insert_params["skip"] is None


def test_toggle_off_computes_price_but_does_not_dispatch(monkeypatch):
    send = MagicMock()
    result, session = _run_decide(monkeypatch, auto_update_price_enabled=False, send_task=send)

    assert result["ok"] is True
    assert result["applied"] is False
    send.assert_not_called()

    insert_params = next(p for sql, p in session.executed if 'INSERT INTO "PriceDecision"' in sql)
    # The price was still fully computed — this is the core requirement.
    assert insert_params["auto"] is False
    assert insert_params["skip"] == "auto_update_off"
    assert insert_params["np"] != insert_params["op"]  # new_price differs from old_price
    assert insert_params["ref"] is not None
    assert insert_params["ftgt"] is not None
    assert insert_params["cu"] == 1  # competitors_used


def test_toggle_off_does_not_override_an_existing_clamp_reason(monkeypatch):
    # Force a per-round clamp by setting a tiny maxAutoApplyChangePct, so
    # skip_reason is already "clamped_per_round" before the toggle gate runs.
    send = MagicMock()
    session = _FakeSession(SimpleNamespace(
        markupPct=0.1, currency="INR", minCompetitorsToPrice=1, topKCompetitors=3,
        maxAutoApplyChangePct=0.01, lifetimeCapPct=0.2, budgetUndercut=0.05,
        premiumUplift=0.05, includeOosInPricing=False,
        autoUpdatePriceEnabled=False,
        frequencyUnit=None, frequencyInterval=None,
        minChangePctThreshold=0.001, minFreshnessHours=24,
    ))
    monkeypatch.setattr(decide_mod, "get_db", lambda: _fake_get_db(session))
    monkeypatch.setattr(decide_mod, "logger", MagicMock())
    monkeypatch.setattr(decide_mod.app, "send_task", send)
    decide_mod.decide_price_for_product("demo.myshopify.com", "prod1")

    insert_params = next(p for sql, p in session.executed if 'INSERT INTO "PriceDecision"' in sql)
    assert insert_params["skip"] == "clamped_per_round"
    assert insert_params["auto"] is False
    send.assert_not_called()


def _run_no_op_case(monkeypatch, auto_update_price_enabled: bool):
    # Competitor price equals the current price and markupPct=0, so
    # ref_price = formula_target = 100 = current price -> step = 0, which is
    # well under the 0.1% minChangePctThreshold. This must hit the no-op
    # branch (skip_reason="no_change") before the toggle gate ever runs.
    settings = SimpleNamespace(
        markupPct=0.0, currency="INR", minCompetitorsToPrice=1, topKCompetitors=3,
        maxAutoApplyChangePct=0.1, lifetimeCapPct=0.2, budgetUndercut=0.05,
        premiumUplift=0.05, includeOosInPricing=False,
        autoUpdatePriceEnabled=auto_update_price_enabled,
        frequencyUnit=None, frequencyInterval=None,
        minChangePctThreshold=0.001, minFreshnessHours=24,
    )
    comp_rows = [SimpleNamespace(
        scraped_product_id="sp1", price=100, currency="INR", in_stock=True,
        observed_at=datetime.now(timezone.utc), match_id="m1", confidence=1.0,
        title="Comp1", domain="comp.com",
    )]
    send = MagicMock()

    class _NoOpSession(_FakeSession):
        def execute(self, stmt, params=None):
            sql = str(stmt)
            self.executed.append((sql, params))
            if "SELECT" in sql and 'FROM "ShopifyProduct"' in sql:
                return _Result(_PRODUCT)
            if 'FROM "ShopSettings"' in sql:
                return _Result(self.settings)
            if 'FROM "ShopifyVariant"' in sql:
                return _Result(_VARIANTS)
            if "WITH latest AS" in sql:
                return _Result(comp_rows)
            if 'INSERT INTO "PriceDecision"' in sql:
                return _Result((_DECISION_ID,))
            return _Result(None)

    session = _NoOpSession(settings)
    monkeypatch.setattr(decide_mod, "get_db", lambda: _fake_get_db(session))
    monkeypatch.setattr(decide_mod, "logger", MagicMock())
    monkeypatch.setattr(decide_mod.app, "send_task", send)
    decide_mod.decide_price_for_product("demo.myshopify.com", "prod1")

    insert_params = next(p for sql, p in session.executed if 'INSERT INTO "PriceDecision"' in sql)
    assert insert_params["skip"] == "no_change"
    assert insert_params["auto"] is False
    send.assert_not_called()


def test_no_op_case_is_unaffected_by_toggle(monkeypatch):
    _run_no_op_case(monkeypatch, auto_update_price_enabled=True)
    _run_no_op_case(monkeypatch, auto_update_price_enabled=False)
