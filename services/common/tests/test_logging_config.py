import json

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
