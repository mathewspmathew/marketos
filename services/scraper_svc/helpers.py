"""
services/scraper_svc/helpers.py

Shared utilities used across scraper, extractor, and semantics tasks.
"""

import os
import uuid

import redis as redis_lib
from sqlalchemy import update as sa_update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from services.common.db import get_db
from services.common.models import ScrapingConfig, ScrapingError

_redis = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

PENDING_KEY_TTL = 7200
URLS_KEY_TTL    = 7200


def update_config_status(config_id: str, status: str) -> None:
    with get_db() as session:
        session.execute(
            sa_update(ScrapingConfig)
            .where(ScrapingConfig.id == config_id)
            .values(status=status, updatedAt=func.now())
        )


def log_error(
    shop_domain: str,
    config_id:   str,
    product_url: str,
    error_type:  str,
    task_name:   str,
    gcs_ref:     str = "",
    detail:      str = "",
) -> None:
    try:
        with get_db() as session:
            session.execute(
                pg_insert(ScrapingError).values(
                    id=str(uuid.uuid4()),
                    shopDomain=shop_domain,
                    configId=config_id,
                    productUrl=product_url,
                    gcsRef=gcs_ref or None,
                    errorType=error_type,
                    errorDetail=detail[:1000] if detail else None,
                    taskName=task_name,
                )
            )
        print(f"    [DLQ] Logged {error_type} for {product_url[:60]}", flush=True)
    except Exception as log_err:
        print(f"    [!] Failed to write DLQ entry: {log_err}", flush=True)


def mark_task_done(config_id: str) -> None:
    try:
        counter_key = f"scrape_pending:{config_id}"
        remaining   = _redis.decr(counter_key)
        print(f"    [>] Pending counter for {config_id}: {remaining}", flush=True)
        if remaining <= 0:
            _redis.delete(counter_key)
            update_config_status(config_id, "SCRAPED_FIRST")
            print(f"    [✓] Config {config_id} → SCRAPED_FIRST", flush=True)
    except Exception as e:
        print(f"    [!] Counter update failed for {config_id}: {e}", flush=True)
