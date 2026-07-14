"""
services/scraper_svc/helpers.py

Shared utilities used across scraper, extractor, and semantics tasks.
"""

import os
from datetime import datetime, timedelta, timezone

import redis as redis_lib
from sqlalchemy import update as sa_update, func

import structlog

from services.common.db import get_db
from services.common.models import ProductUrl, ScrapingConfig

logger = structlog.get_logger(__name__)

_UNIT_TO_SECONDS = {"min": 60, "hr": 3600, "day": 86400}

_redis = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

PENDING_KEY_TTL = 7200
URLS_KEY_TTL    = 7200


def update_config_status(config_id: str, status: str) -> None:
    try:
        with get_db() as session:
            session.execute(
                sa_update(ScrapingConfig)
                .where(ScrapingConfig.id == config_id)
                .values(status=status, updatedAt=func.now())
            )
    except Exception:
        logger.exception("update_config_status_failed", config_id=config_id, status=status)


def log_error(
    shop_domain: str,
    config_id:   str,
    product_url: str,
    error_type:  str,
    task_name:   str,
    gcs_ref:     str = "",
    detail:      str = "",
) -> None:
    logger.error(
        "task_error",
        error_type=error_type,
        task_name=task_name,
        shop_domain=shop_domain,
        config_id=config_id,
        product_url=product_url,
        gcs_ref=gcs_ref or None,
        detail=(detail or "")[:300] or None,
    )


def set_next_scrap_at(config_id: str, product_url: str) -> None:
    """Set ProductUrl.nextRunAt based on config frequency. No-op if nofreq."""
    try:
        with get_db() as session:
            config = session.query(ScrapingConfig).filter(ScrapingConfig.id == config_id).first()
            if not config:
                return
            unit     = config.frequencyUnit or "nofreq"
            interval = config.frequencyInterval or 1
            if unit not in _UNIT_TO_SECONDS:
                return
            next_at = datetime.now(timezone.utc) + timedelta(seconds=interval * _UNIT_TO_SECONDS[unit])
            session.execute(
                sa_update(ProductUrl)
                .where(ProductUrl.url == product_url)
                .values(nextRunAt=next_at)
            )
            logger.info("next_run_at_set", product_url=product_url, next_run_at=next_at.isoformat())
    except Exception:
        logger.exception("set_next_scrap_at_failed", config_id=config_id, product_url=product_url)


def mark_task_done(config_id: str) -> None:
    try:
        counter_key = f"scrape_pending:{config_id}"
        if not _redis.exists(counter_key):
            return  # re-scrape path — no initial-scrape counter to manage
        remaining   = _redis.decr(counter_key)
        logger.info("pending_counter_decremented", config_id=config_id, remaining=remaining)
        if remaining <= 0:
            _redis.delete(counter_key)
            update_config_status(config_id, "SCRAPED_FIRST")
            logger.info("config_scraped_first", config_id=config_id)
    except Exception:
        logger.exception("mark_task_done_failed", config_id=config_id)
