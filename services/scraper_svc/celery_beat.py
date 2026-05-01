from datetime import datetime, timedelta, timezone

from sqlalchemy import update as sa_update, func

from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ScrapingConfig

_STUCK_TIMEOUT_HOURS = 1


@app.task(name='services.scraper_svc.celery_beat.check_idle_configs')
def check_idle_configs():
    print("[Beat] Polling for IDLE configs...", flush=True)
    with get_db() as session:
        # Reset configs stuck in QUEUED/RUNNING for more than _STUCK_TIMEOUT_HOURS
        stuck_cutoff = datetime.now(timezone.utc) - timedelta(hours=_STUCK_TIMEOUT_HOURS)
        # this is used because - if we just set the stuck configs to IDLE for 
        # (RUNNING status) or(QUEUED status) - but haven't updated their status in the DB yet. - orphan ones
        stuck = (
            session.query(ScrapingConfig)
            .filter(
                ScrapingConfig.status.in_(["QUEUED", "RUNNING"]),
                ScrapingConfig.isActive == True,
                ScrapingConfig.updatedAt < stuck_cutoff,
            )
            .all()
        )
        
        # making the stuck ones to IDLE so that they can be picked up in the next beat cycle and processed.
        # sa_update is an alias for sqlalchemy's update function - perform an atomic update on the database.
        for config in stuck:
            print(f"[Beat] Stuck config {config.id} ({config.status} >{_STUCK_TIMEOUT_HOURS}h) → IDLE", flush=True)
            session.execute(
                sa_update(ScrapingConfig)
                .where(ScrapingConfig.id == config.id)
                .values(status="IDLE", updatedAt=func.now())
            )

        # Queue IDLE configs — update to QUEUED atomically before sending to
        # prevent a second beat firing within the same 30s window from
        # picking up the same config.
        
        # original logic ->
        
        #taking all IDLE rows
        configs = (
            session.query(ScrapingConfig)
            .filter(ScrapingConfig.status == "IDLE", ScrapingConfig.isActive == True)
            .all()
        )
        
        for config in configs:
            # Atomic status flip: only proceeds if status is still IDLE
            result = session.execute(
                sa_update(ScrapingConfig)
                .where(ScrapingConfig.id == config.id, ScrapingConfig.status == "IDLE")
                .values(status="QUEUED", updatedAt=func.now())
            )
            if result.rowcount == 0:
                # this condition - if worker A and worker B - pick same IDLE job - both try to update to QUEUED - but only one will succeed - the other will get rowcount 0 - so we can skip the one which got rowcount 0 because it means another worker already claimed it.
                # Another beat invocation already claimed this config
                continue

            print(f"[Beat] Queuing scrape for Config {config.id}", flush=True)
            try:
                app.send_task(
                    'scraper.scrape_listing',
                    args=[config.id, config.userId, config.competitorUrl, config.productLimit or 5],
                    queue='scraping_queue',
                )
            except Exception as e:
                print(f"[Beat] Failed to queue Config {config.id}: {e}", flush=True)
                session.execute(
                    sa_update(ScrapingConfig)
                    .where(ScrapingConfig.id == config.id)
                    .values(status="IDLE", updatedAt=func.now())
                )
