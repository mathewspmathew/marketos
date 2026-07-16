import os
os.environ.setdefault("GROQ_API_KEY", "test")

import json
import logging

import pytest

from services.common.db import get_db
from services.common.models import ShopifyProduct
from services.common import logging_config
from services.chatbot_svc.schemas import PaneConfigInput
from services.chatbot_svc.tools import apply_config as t_apply_config


def _blank_config(**overrides):
    defaults = dict(
        search_query_override=None,
        pricing_tier=None,
        min_price_override=None,
        max_price_override=None,
        frequency_unit=None,
        frequency_interval=None,
        discovery_num_results=None,
        listing_expansion_cap=None,
    )
    defaults.update(overrides)
    return PaneConfigInput(**defaults)


def _product_id(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(ShopifyProduct.shopDomain == shop).scalar()


@pytest.fixture(autouse=True)
def _restore_root_logger_handlers():
    """setup_logging() replaces the root logger's handlers wholesale with no
    teardown of its own — restore whatever was there before this test so a
    handler bound to this test's now-closed capsys stream doesn't leak into
    later test files. Safe to snapshot/restore here (fixture setup/teardown)
    because it only reads/replaces the handler list; it never binds a new
    handler to capsys itself — that still has to happen inside the test body
    via _reset_json_logging(), for the phase-timing reason documented there."""
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level
    yield
    root_logger.handlers = original_handlers
    root_logger.level = original_level


def _reset_json_logging() -> None:
    """Reuse the same setup_logging() every Celery worker calls — no second
    config. Called at the top of each test (not via an autouse fixture)
    because pytest's capsys gives a distinct stdout-capture object per test
    phase; binding the StreamHandler during fixture setup would leave it
    pointed at a stream that's already closed by the time the test body's
    log calls and capsys.readouterr() run in the call phase."""
    logging_config.setup_logging()


def _last_log_lines(capsys, n=1):
    out = capsys.readouterr().out.strip().splitlines()
    return [json.loads(line) for line in out[-n:]]


def test_apply_missing_fields_logs_warning(seed_shop, capsys):
    # seed_shop's product starts with dynamicPricingEnabled=False and no
    # frequencyUnit/pricingTier set — an all-null config hits the
    # first-enable missing-fields gate.
    _reset_json_logging()
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError):
        t_apply_config.apply_dynamic_pricing_config(seed_shop, pid, _blank_config())

    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_apply_missing_fields"]
    assert len(matches) == 1
    assert matches[0]["level"] == "warning"
    assert matches[0]["shop_domain"] == seed_shop
    assert matches[0]["product_id"] == pid
    assert "missing" in matches[0]


def test_apply_success_logs_info(seed_shop, capsys):
    _reset_json_logging()
    pid = _product_id(seed_shop)
    t_apply_config.apply_dynamic_pricing_config(
        seed_shop, pid,
        _blank_config(
            pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6,
        ),
    )
    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_applied"]
    assert len(matches) == 1
    assert matches[0]["level"] == "info"
    assert matches[0]["pricing_tier"] == "PREMIUM"
    assert matches[0]["rearmed_count"] is not None
    assert matches[0]["enabled_before"] is False
    assert matches[0]["enabled_after"] is True


def test_pause_success_logs_info(seed_shop, capsys):
    _reset_json_logging()
    pid = _product_id(seed_shop)
    t_apply_config.apply_dynamic_pricing_config(
        seed_shop, pid,
        _blank_config(pricing_tier="BUDGET", frequency_unit="day", frequency_interval=1),
    )
    capsys.readouterr()  # discard the apply's own log line
    t_apply_config.pause_dynamic_pricing(seed_shop, pid)
    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_paused"]
    assert len(matches) == 1
    assert matches[0]["level"] == "info"
    assert matches[0]["enabled_before"] is True
    assert matches[0]["enabled_after"] is False


def test_delete_not_confirmed_logs_warning(seed_shop, capsys):
    _reset_json_logging()
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError):
        t_apply_config.delete_dynamic_pricing(seed_shop, pid, confirmed=False)
    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_delete_not_confirmed"]
    assert len(matches) == 1
    assert matches[0]["level"] == "warning"


def test_delete_success_logs_info(seed_shop, capsys):
    _reset_json_logging()
    pid = _product_id(seed_shop)
    t_apply_config.delete_dynamic_pricing(seed_shop, pid, confirmed=True)
    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_deleted"]
    assert len(matches) == 1
    assert matches[0]["level"] == "info"
    assert "deleted_scraped_products" in matches[0]


def test_product_not_found_logs_warning(capsys):
    _reset_json_logging()
    with pytest.raises(RuntimeError):
        t_apply_config.pause_dynamic_pricing("no-such-shop.myshopify.com", "no-such-product")
    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_product_not_found"]
    assert len(matches) == 1
    assert matches[0]["level"] == "warning"


def test_apply_unexpected_exception_logs_error_and_hides_internal_message(seed_shop, capsys, monkeypatch):
    _reset_json_logging()
    pid = _product_id(seed_shop)

    def _boom(*args, **kwargs):
        raise ValueError("some internal SQL detail merchants must never see")

    monkeypatch.setattr(t_apply_config, "apply_pane_config", _boom)

    with pytest.raises(RuntimeError) as excinfo:
        t_apply_config.apply_dynamic_pricing_config(
            seed_shop, pid,
            _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
        )

    assert "some internal SQL detail" not in str(excinfo.value)

    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_apply_failed"]
    assert len(matches) == 1
    assert matches[0]["level"] == "error"
    assert "some internal SQL detail" in matches[0]["error"]
