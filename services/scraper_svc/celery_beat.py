from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import distinct, update as sa_update, func, text

from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ProductUrl, ScrapingConfig, ShopifyVariant

_STUCK_TIMEOUT_HOURS = 1


_RESCRAPE_DOMAIN_GAP = 30  # seconds between consecutive scrapes of the same domain

# Cap how many rescrape tasks we dispatch per beat tick. Dispatching every due
# URL at once creates a long tail of pending tasks and a stale queue head if
# the score-rank changes mid-queue. Score-rank the top-N each tick and let
# overflow get re-scored on the next tick.
_MAX_RESCRAPES_PER_TICK = 50


def _rescrape_pass() -> None:
    """Score-rank due ProductUrls and dispatch the top N per tick.

    Score = revenue_weight × volatility_factor × staleness_factor
      revenue_weight    = 1 + (Σ revenue7d over merchant variants whose product
                          has a non-rejected ProductLevelMatch to this scraped
                          product) / 1000. Roughly: "how much merchant money
                          rides on knowing this competitor's price."
      volatility_factor = 1 + avg(VariantCompetitorStats.volatility24h) over
                          merchant variants matched to this competitor product.
                          Volatile competitors deserve more frequent refresh.
      staleness_factor  = max(1.0, hours_past_due) — climbs as a URL ages so
                          nothing starves indefinitely.

    URLs with no match / no stats / no sales get a 1.0 factor for that term
    (the COALESCEs), so brand-new configs default to staleness-only ordering
    — identical to the old behaviour.

    Per-domain stagger keeps same-site request rate constant; only the
    dispatch ORDER changes.
    """
    with get_db() as session:
        rows = session.execute(
            text("""
                WITH due AS (
                    SELECT pu.id           AS url_id,
                           pu.url          AS url,
                           pu."prodId"     AS prod_id,
                           pu."nextScrapAt" AS next_scrap_at,
                           sc.id           AS config_id,
                           sc."shopDomain" AS shop_domain
                    FROM "ProductUrl" pu
                    JOIN "ScrapingConfig" sc ON sc.id = pu."configId"
                    WHERE sc.status = 'SCRAPED_FIRST'
                      AND sc."isActive" = TRUE
                      AND pu.status = 'ACTIVE'
                      AND pu."nextScrapAt" IS NOT NULL
                      AND pu."nextScrapAt" <= NOW()
                ),
                rev AS (
                    -- Aggregate merchant-side 7d revenue tied to this URL via
                    -- ProductLevelMatch → ShopifyProduct → ShopifyVariant →
                    -- SalesAggregate. Skips rejected pairs so we don't waste
                    -- rescrape budget on competitors the merchant rejected.
                    SELECT d.url_id,
                           COALESCE(SUM(sa."revenue7d"), 0) AS rev7d
                    FROM due d
                    LEFT JOIN "ProductLevelMatch" plm
                        ON plm."scrapedProductId"    = d.prod_id
                       AND plm."shopDomain"          = d.shop_domain
                       AND plm."rejectedByMerchant"  = FALSE
                    LEFT JOIN "ShopifyVariant" sv
                        ON sv."productId" = plm."shopifyProductId"
                    LEFT JOIN "SalesAggregate" sa
                        ON sa."shopifyVariantId" = sv.id
                    GROUP BY d.url_id
                ),
                vol AS (
                    -- Average volatility24h across merchant variants whose
                    -- current stats reflect competitor variants of this URL's
                    -- competitor product. Joined via ProductMatch.
                    SELECT d.url_id,
                           COALESCE(AVG(vcs."volatility24h"), 0)::float AS vol24
                    FROM due d
                    LEFT JOIN "ScrapedVariant" csv
                        ON csv."productId" = d.prod_id
                    LEFT JOIN "ProductMatch" pm
                        ON pm."competitorVariantId" = csv.id
                       AND pm."shopDomain"          = d.shop_domain
                    LEFT JOIN "VariantCompetitorStats" vcs
                        ON vcs."shopifyVariantId" = pm."shopifyVariantId"
                    GROUP BY d.url_id
                )
                SELECT d.url_id, d.url, d.prod_id, d.config_id, d.shop_domain,
                       d.next_scrap_at,
                       COALESCE(rev.rev7d, 0)::float                  AS rev7d,
                       COALESCE(vol.vol24, 0)::float                  AS vol24,
                       GREATEST(1.0,
                           EXTRACT(EPOCH FROM (NOW() - d.next_scrap_at)) / 3600.0
                       )                                              AS staleness,
                       (1.0 + COALESCE(rev.rev7d, 0)::float / 1000.0)
                       * (1.0 + COALESCE(vol.vol24, 0)::float)
                       * GREATEST(1.0,
                             EXTRACT(EPOCH FROM (NOW() - d.next_scrap_at)) / 3600.0
                         )                                            AS score
                FROM due d
                LEFT JOIN rev ON rev.url_id = d.url_id
                LEFT JOIN vol ON vol.url_id = d.url_id
                ORDER BY score DESC, d.next_scrap_at ASC
                LIMIT :lim
            """),
            {"lim": _MAX_RESCRAPES_PER_TICK},
        ).all()

        if not rows:
            return

        # Hydrate only the configs we plan to dispatch (avoids reading every
        # ScrapingConfig in the shop when only a few URLs are due).
        config_ids = list({r.config_id for r in rows})
        configs = {
            c.id: c for c in session.query(ScrapingConfig)
                .filter(ScrapingConfig.id.in_(config_ids))
                .all()
        }

        # Per-domain countdown: highest-scoring URL in a domain fires first
        # (0s), next +30s, etc. — preserves the same-site rate limit while
        # giving priority to score-ranked URLs.
        domain_next_countdown: dict[str, int] = {}

        for r in rows:
            config = configs.get(r.config_id)
            if config is None:
                continue
            domain    = urlparse(r.url).netloc
            countdown = domain_next_countdown.get(domain, 0)
            domain_next_countdown[domain] = countdown + _RESCRAPE_DOMAIN_GAP

            print(
                f"[Beat] Rescrape +{countdown}s: {r.url[:60]} "
                f"(domain={domain} score={r.score:.2f} "
                f"rev7d={r.rev7d:.0f} vol24={r.vol24:.3f} stale={r.staleness:.1f}h)",
                flush=True,
            )
            try:
                app.send_task(
                    'scraper.rescrape_product',
                    args=[config.id, config.shopDomain, r.url, r.prod_id],
                    queue='scraping_queue',
                    countdown=countdown,
                )
                session.execute(
                    sa_update(ProductUrl)
                    .where(ProductUrl.id == r.url_id)
                    .values(nextScrapAt=None)
                )
            except Exception as e:
                print(f"[Beat] Failed to schedule rescrape for {r.url[:60]}: {e}", flush=True)


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
                queue='shopify_semantic_queue',
            )
        except Exception as e:
            print(f"[Beat] Failed to queue Shopify semantics for {product_id}: {e}", flush=True)


