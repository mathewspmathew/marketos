import os
os.environ.setdefault("GROQ_API_KEY", "test")

import json
import logging
import sys

import pytest
import structlog

from services.common.db import get_db
from services.common.models import ShopifyProduct
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


class CaptureHandler(logging.StreamHandler):
    """Custom handler that also stores formatted messages for easy access."""
    def __init__(self, stream):
        super().__init__(stream)
        self.messages = []

    def emit(self, record):
        try:
            msg = self.format(record)
            self.messages.append(msg)
            super().emit(record)
        except Exception:
            self.handleError(record)


@pytest.fixture(autouse=True)
def _json_logging(capsys):
    """Route structlog through the real JSON stdout pipeline for this test
    file, so we can assert on the actual rendered event/level/fields."""
    # Clear any existing handlers to ensure clean state
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    # Configure structlog processors
    _SHARED_PROCESSORS = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=_SHARED_PROCESSORS
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,  # Don't cache so we get fresh loggers
    )

    # Create formatter
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
    )

    # Use custom handler that stores messages and also writes to stdout
    handler = CaptureHandler(sys.stdout)
    handler.setFormatter(formatter)

    # Set up root logger with this handler only
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.INFO)

    # Store handler on capsys so _last_log_lines can access it
    capsys._json_handler = handler

    yield capsys


def _last_log_lines(capsys, n=1):
    # Get messages from the custom handler we installed in the fixture
    if hasattr(capsys, '_json_handler'):
        messages = capsys._json_handler.messages[-n:]
        return [json.loads(msg) for msg in messages if msg.strip()]
    # Fallback to reading from capsys (in case handler is not available)
    out = capsys.readouterr().out.strip().splitlines()
    return [json.loads(line) for line in out[-n:]]


def test_apply_missing_fields_logs_warning(seed_shop, capsys):
    # seed_shop's product starts with dynamicPricingEnabled=False and no
    # frequencyUnit/pricingTier set — an all-null config hits the
    # first-enable missing-fields gate.
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
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError):
        t_apply_config.delete_dynamic_pricing(seed_shop, pid, confirmed=False)
    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_delete_not_confirmed"]
    assert len(matches) == 1
    assert matches[0]["level"] == "warning"


def test_delete_success_logs_info(seed_shop, capsys):
    pid = _product_id(seed_shop)
    t_apply_config.delete_dynamic_pricing(seed_shop, pid, confirmed=True)
    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_deleted"]
    assert len(matches) == 1
    assert matches[0]["level"] == "info"
    assert "deleted_scraped_products" in matches[0]


def test_product_not_found_logs_warning(capsys):
    with pytest.raises(RuntimeError):
        t_apply_config.pause_dynamic_pricing("no-such-shop.myshopify.com", "no-such-product")
    lines = _last_log_lines(capsys, 5)
    matches = [l for l in lines if l["event"] == "dynamic_pricing_product_not_found"]
    assert len(matches) == 1
    assert matches[0]["level"] == "warning"


def test_apply_unexpected_exception_logs_error_and_hides_internal_message(seed_shop, capsys, monkeypatch):
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
