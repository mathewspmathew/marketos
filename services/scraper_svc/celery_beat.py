"""
services/scraper_svc/celery_beat.py

Beat tick (every 30s, see celery_app.beat_schedule):
  - Dispatch scraper.rescrape_url for product-rooted URLs that are due.
  - Dispatch discovery.search_products for queued discovery jobs (UI-driven).
  - Backfill semantic generation for any ShopifyVariant missing semanticText.

The discovery → scrape_candidate → extract_candidate → ProductUrl path is the
primary flow. Beat dispatches rescraping based on ProductUrl.nextRunAt schedule.
"""
from urllib.parse import urlparse

from sqlalchemy import update as sa_update, text

from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ProductUrl, ShopifyVariant

_STUCK_TIMEOUT_HOURS    = 1
_RESCRAPE_DOMAIN_GAP    = 30   # seconds between consecutive scrapes of the same domain
_MAX_RESCRAPES_PER_TICK = 50   # bounded fan-out so a backlog can't blow the queue


def _tick_product_urls() -> None:
    """Dispatch scraper.rescrape_url for product-rooted URLs that are due.

    Ordering: oldest nextRunAt first (FIFO on staleness). Per-domain
    countdown preserves the same-site request gap. Fan-out is capped per
    tick so a sudden surge of due URLs doesn't dominate the queue.
    """
    with get_db() as session:
        rows = session.execute(
            text("""
                SELECT pu.id, pu.url, pu."nextRunAt"
                FROM "ProductUrl" pu
                LEFT JOIN "ShopifyProduct" sp ON sp.id = pu."shopifyProductId"
                LEFT JOIN "ShopSettings" ss ON ss."shopDomain" = pu."shopDomain"
                WHERE pu.status = 'ACTIVE'
                  AND pu."nextRunAt" IS NOT NULL
                  AND pu."nextRunAt" <= NOW()
                  AND pu."shopifyProductId" IS NOT NULL
                  AND sp."dynamicPricingEnabled" = TRUE
                  AND COALESCE(sp."frequencyUnit", 'never') <> 'never'
                  AND COALESCE(ss."autoRescrapeEnabled", TRUE) = TRUE
                ORDER BY pu."nextRunAt" ASC
                LIMIT :lim
            """),
            {"lim": _MAX_RESCRAPES_PER_TICK},
        ).all()

        if not rows:
            return

        domain_next_countdown: dict[str, int] = {}
        for r in rows:
            domain    = urlparse(r.url).netloc
            countdown = domain_next_countdown.get(domain, 0)
            domain_next_countdown[domain] = countdown + _RESCRAPE_DOMAIN_GAP

            print(
                f"[Beat] rescrape_url +{countdown}s id={r.id[:8]} "
                f"due={r.nextRunAt.isoformat() if r.nextRunAt else '-'} "
                f"url={r.url[:60]}",
                flush=True,
            )
            try:
                app.send_task(
                    "scraper.rescrape_url",
                    args=[r.id],
                    queue="scraping_queue",
                    countdown=countdown,
                )
                # Clear nextRunAt so a second beat firing within the dispatch
                # window doesn't double-queue the same URL. rescrape_url
                # resets nextRunAt on success.
                session.execute(
                    sa_update(ProductUrl)
                    .where(ProductUrl.id == r.id)
                    .values(nextRunAt=None)
                )
            except Exception as exc:
                print(f"[Beat] dispatch failed for ProductUrl {r.id}: {exc}", flush=True)


@app.task(name='services.scraper_svc.celery_beat.check_idle_configs')
def check_idle_configs():
    """Beat entry point. Name kept for compatibility with existing schedule."""
    print("[Beat] tick", flush=True)
    _tick_queued_discovery_jobs()
    _tick_product_urls()
    _shopify_semantic_backfill()


def _tick_queued_discovery_jobs() -> None:
    """Pick up DiscoveryJob rows the UI inserted as QUEUED and dispatch them.

    Discovery is now merchant-driven (one search per click) rather than
    auto-triggered on dynamic-pricing toggle. The UI writes a row with the
    refined query + result count; this tick fans them out to the worker.
    """
    with get_db() as session:
        # Atomic claim: flip QUEUED → RUNNING in the same statement so an
        # overlapping beat tick can't re-dispatch the same job. The discovery
        # worker still flips RUNNING → COMPLETED / FAILED when it finishes.
        rows = session.execute(
            text("""
                UPDATE "DiscoveryJob"
                SET status = 'RUNNING'
                WHERE id IN (
                    SELECT id FROM "DiscoveryJob"
                    WHERE status = 'QUEUED'
                      AND query IS NOT NULL
                    ORDER BY "requestedAt" ASC
                    LIMIT 20
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, "shopifyProductId", query
            """),
        ).all()

    for r in rows:
        try:
            app.send_task(
                "discovery.search_products",
                args=[r.shopifyProductId, r.query],
                queue="discovery_queue",
            )
            print(f"[Beat] enqueued discovery job {r.id[:8]} q={r.query!r}", flush=True)
        except Exception as exc:
            print(f"[Beat] dispatch failed for discovery {r.id}: {exc}", flush=True)


def _shopify_semantic_backfill() -> None:
    """Safety-net: claim any PENDING (or stale-QUEUED) ShopifyProduct and enqueue
    one semantic task each. Bounded by the claim — products already QUEUED are
    never re-enqueued, so the queue cannot grow without bound."""
    from services.scraper_svc.semantics import claim_and_enqueue_semantics
    with get_db() as session:
        claimed = claim_and_enqueue_semantics(session, ids=None)
    if claimed:
        print(f"[Beat] semantic backfill: claimed {len(claimed)} product(s)", flush=True)
