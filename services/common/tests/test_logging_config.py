import json
import logging
import sys

import structlog

from services.common import logging_config


def test_setup_logging_emits_json_with_expected_keys(capsys):
    logging_config.setup_logging()
    logger = structlog.get_logger("test.logging_config")

    logger.info("something_happened", foo="bar")

    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["event"] == "something_happened"
    assert payload["foo"] == "bar"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_task_prerun_binds_and_postrun_clears_context():
    class FakeTask:
        name = "pricing.decide_for_product"

    logging_config._bind_task_context(task_id="abc-123", task=FakeTask())
    bound = structlog.contextvars.get_contextvars()
    assert bound["task_id"] == "abc-123"
    assert bound["task_name"] == "pricing.decide_for_product"

    logging_config._clear_task_context()
    assert structlog.contextvars.get_contextvars() == {}


def test_celery_setup_logging_signal_preserves_structured_stdout_logging(capsys):
    logging_config.setup_logging()

    # Simulate what a real Celery worker boot would otherwise do: replace the
    # root logger's handler with its own plain-text stderr handler. If our
    # signal receiver is connected, Celery skips this internal step — but we
    # emulate the clobbering here to prove the receiver actually restores
    # structured stdout logging afterwards.
    root_logger = logging.getLogger()
    root_logger.handlers = [logging.StreamHandler(sys.stderr)]

    logging_config._on_celery_setup_logging()

    assert len(root_logger.handlers) == 1
    handler = root_logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout

    logger = structlog.get_logger("test.logging_config.celery_boot")
    logger.info("celery_worker_booted", queue="pricing_queue")

    captured = capsys.readouterr()
    line = captured.out.strip().splitlines()[-1]
    payload = json.loads(line)

    assert payload["event"] == "celery_worker_booted"
    assert payload["queue"] == "pricing_queue"
    assert payload["level"] == "info"
    assert "timestamp" in payload
    assert captured.err == ""
