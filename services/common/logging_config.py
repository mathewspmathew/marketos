"""Structured JSON logging setup, shared by every Celery worker and beat.

Call setup_logging() once at process startup (celery_app.py does this at
import time). Everything logged afterwards — via structlog.get_logger() or
via stdlib logging.getLogger(), including Celery's own internal logs — is
rendered as a single JSON line on stdout.
"""
from __future__ import annotations

import logging
import os
import sys

import structlog
from celery.signals import task_postrun, task_prerun

_SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
]


def setup_logging() -> None:
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    structlog.configure(
        processors=_SHARED_PROCESSORS
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)


@task_prerun.connect
def _bind_task_context(task_id=None, task=None, **_kwargs) -> None:
    structlog.contextvars.bind_contextvars(
        task_id=task_id,
        task_name=getattr(task, "name", None),
    )


@task_postrun.connect
def _clear_task_context(**_kwargs) -> None:
    structlog.contextvars.clear_contextvars()
