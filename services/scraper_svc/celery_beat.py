from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import distinct, update as sa_update, func

from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ProductUrl, ScrapingConfig, ShopifyUser, ShopifyVariant

_STUCK_TIMEOUT_HOURS = 1


_RESCRAPE_DOMAIN_GAP = 30  # seconds between consecutive scrapes of the same domain


def _rescrape_pass() -> None:
    """Queue ALL due ProductUrls. Stagger per domain with countdown so the
    same site is never hit concurrently — oldest due URL fires first."""
    now = datetime.now(timezone.utc)
    with get_db() as session:
        due_urls = (
            session.query(ProductUrl, ScrapingConfig)
            .join(ScrapingConfig, ProductUrl.configId == ScrapingConfig.id)
            .filter(
                ScrapingConfig.status == "SCRAPED_FIRST",
                ScrapingConfig.isActive == True,
                ProductUrl.status == "ACTIVE",
                ProductUrl.nextScrapAt != None,
                ProductUrl.nextScrapAt <= now,
            )
            .order_by(ProductUrl.nextScrapAt.asc())  # oldest due first
            .all()
        )

        if not due_urls:
            return

        # Track per-domain countdown so same-site tasks are spaced _RESCRAPE_DOMAIN_GAP apart
        domain_next_countdown: dict[str, int] = {}

        for pu, config in due_urls:
            domain   = urlparse(pu.url).netloc
            countdown = domain_next_countdown.get(domain, 0)
            domain_next_countdown[domain] = countdown + _RESCRAPE_DOMAIN_GAP

            print(
                f"[Beat] Scheduling rescrape in {countdown}s: {pu.url[:60]} (domain={domain})",
                flush=True,
            )
            try:
                app.send_task(
                    'scraper.rescrape_product',
                    args=[config.id, config.shopDomain, pu.url, pu.prodId],
                    queue='scraping_queue',
                    countdown=countdown,
                )
                # Clear nextScrapAt immediately — task sets it again on completion
                session.execute(
                    sa_update(ProductUrl)
                    .where(ProductUrl.id == pu.id)
                    .values(nextScrapAt=None)
                )
            except Exception as e:
                print(f"[Beat] Failed to schedule rescrape for {pu.url[:60]}: {e}", flush=True)


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
                    args=[config.id, config.shopDomain, config.competitorUrl, config.productLimit or 5],
                    queue='scraping_queue',
                )
            except Exception as e:
                print(f"[Beat] Failed to queue Config {config.id}: {e}", flush=True)
                session.execute(
                    sa_update(ScrapingConfig)
                    .where(ScrapingConfig.id == config.id)
                    .values(status="IDLE", updatedAt=func.now())
                )

    _rescrape_pass()
    _shopify_semantic_backfill()


def _shopify_semantic_backfill() -> None:
    """Queue semantic generation for any ShopifyVariant still missing semanticText.
    Recovers products whose webhook fired while the API gateway was down."""
    with get_db() as session:
        product_ids = [
            row[0]
            for row in session.query(distinct(ShopifyVariant.productId))
            .filter(ShopifyVariant.semanticText == None)  # noqa: E711
            .all()
        ]

    if not product_ids:
        return

    print(f"[Beat] Shopify backfill: queuing semantics for {len(product_ids)} product(s)", flush=True)
    for product_id in product_ids:
        try:
            app.send_task(
                'scraper.generate_shopify_variant_semantics',
                args=[product_id],
                queue='semantic_queue',
            )
        except Exception as e:
            print(f"[Beat] Failed to queue Shopify semantics for {product_id}: {e}", flush=True)


@app.task(name='services.scraper_svc.celery_beat.matcher_sweep')
def matcher_sweep():
    """Nightly safety net: re-match every shop in case event-driven triggers were missed."""
    with get_db() as session:
        shop_domains = [r[0] for r in session.query(ShopifyUser.shopDomain).all()]

    if not shop_domains:
        return

    print(f"[Beat] matcher sweep: queuing match_for_shop for {len(shop_domains)} shop(s)", flush=True)
    for shop_domain in shop_domains:
        try:
            app.send_task(
                'matcher.match_for_shop',
                args=[shop_domain, True],
                queue='match_queue',
            )
        except Exception as e:
            print(f"[Beat] Failed to queue matcher_sweep for {shop_domain}: {e}", flush=True)


