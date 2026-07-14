import os
os.environ.setdefault("GROQ_API_KEY", "test")

import json

import pytest

from services.common.db import get_db
from services.common.models import ShopifyProduct
from services.common import logging_config
from services.chatbot_svc.schemas import PaneConfigInput
from services.chatbot_svc.tools import apply_config as t_apply_config

# Set up JSON logging at module import time, before pytest runs any tests
logging_config.setup_logging()


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


def _get_structlog_records(caplog):
    """Extract structlog records from pytest's caplog records.

    When structlog logs through stdlib logging with the ProcessorFormatter,
    the entire structured dict is packed into record.msg.
    """
    records = []

    for record in caplog.records:
        # structlog packs the entire structured dict into record.msg
        if isinstance(record.msg, dict):
            parsed = dict(record.msg)
            # Ensure level is lowercase
            if "level" in parsed:
                parsed["level"] = parsed["level"].lower()
        else:
            # Fallback: build from record attributes
            parsed = {
                "event": str(record.msg),
                "level": record.levelname.lower(),
            }

        records.append(parsed)

    return records


def test_apply_missing_fields_logs_warning(seed_shop, caplog):
    # seed_shop's product starts with dynamicPricingEnabled=False and no
    # frequencyUnit/pricingTier set — an all-null config hits the
    # first-enable missing-fields gate.
    pid = _product_id(seed_shop)
    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError):
            t_apply_config.apply_dynamic_pricing_config(seed_shop, pid, _blank_config())

    # Extract the structured log records
    records = _get_structlog_records(caplog)
    matches = [r for r in records if r.get("event") == "dynamic_pricing_apply_missing_fields"]
    assert len(matches) == 1
    assert matches[0]["level"] == "warning"
    assert matches[0]["shop_domain"] == seed_shop
    assert matches[0]["product_id"] == pid
    assert "missing" in matches[0]


def test_apply_success_logs_info(seed_shop, caplog):
    pid = _product_id(seed_shop)
    with caplog.at_level("INFO"):
        t_apply_config.apply_dynamic_pricing_config(
            seed_shop, pid,
            _blank_config(
                pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6,
            ),
        )
    records = _get_structlog_records(caplog)
    matches = [r for r in records if r.get("event") == "dynamic_pricing_applied"]
    assert len(matches) == 1
    assert matches[0]["level"] == "info"
    assert matches[0]["pricing_tier"] == "PREMIUM"
    assert matches[0]["rearmed_count"] is not None
    assert matches[0]["enabled_before"] is False
    assert matches[0]["enabled_after"] is True


def test_pause_success_logs_info(seed_shop, caplog):
    pid = _product_id(seed_shop)
    with caplog.at_level("INFO"):
        t_apply_config.apply_dynamic_pricing_config(
            seed_shop, pid,
            _blank_config(pricing_tier="BUDGET", frequency_unit="day", frequency_interval=1),
        )
        caplog.clear()  # discard the apply's own log line
        t_apply_config.pause_dynamic_pricing(seed_shop, pid)
    records = _get_structlog_records(caplog)
    matches = [r for r in records if r.get("event") == "dynamic_pricing_paused"]
    assert len(matches) == 1
    assert matches[0]["level"] == "info"
    assert matches[0]["enabled_before"] is True
    assert matches[0]["enabled_after"] is False


def test_delete_not_confirmed_logs_warning(seed_shop, caplog):
    pid = _product_id(seed_shop)
    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError):
            t_apply_config.delete_dynamic_pricing(seed_shop, pid, confirmed=False)
    records = _get_structlog_records(caplog)
    matches = [r for r in records if r.get("event") == "dynamic_pricing_delete_not_confirmed"]
    assert len(matches) == 1
    assert matches[0]["level"] == "warning"


def test_delete_success_logs_info(seed_shop, caplog):
    pid = _product_id(seed_shop)
    with caplog.at_level("INFO"):
        t_apply_config.delete_dynamic_pricing(seed_shop, pid, confirmed=True)
    records = _get_structlog_records(caplog)
    matches = [r for r in records if r.get("event") == "dynamic_pricing_deleted"]
    assert len(matches) == 1
    assert matches[0]["level"] == "info"
    assert "deleted_scraped_products" in matches[0]


def test_product_not_found_logs_warning(caplog):
    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError):
            t_apply_config.pause_dynamic_pricing("no-such-shop.myshopify.com", "no-such-product")
    records = _get_structlog_records(caplog)
    matches = [r for r in records if r.get("event") == "dynamic_pricing_product_not_found"]
    assert len(matches) == 1
    assert matches[0]["level"] == "warning"


def test_apply_unexpected_exception_logs_error_and_hides_internal_message(seed_shop, caplog, monkeypatch):
    pid = _product_id(seed_shop)

    def _boom(*args, **kwargs):
        raise ValueError("some internal SQL detail merchants must never see")

    monkeypatch.setattr(t_apply_config, "apply_pane_config", _boom)

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError) as excinfo:
            t_apply_config.apply_dynamic_pricing_config(
                seed_shop, pid,
                _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
            )

    assert "some internal SQL detail" not in str(excinfo.value)

    records = _get_structlog_records(caplog)
    matches = [r for r in records if r.get("event") == "dynamic_pricing_apply_failed"]
    assert len(matches) == 1
    assert matches[0]["level"] == "error"
    assert "some internal SQL detail" in matches[0]["error"]
