import os
from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv

load_dotenv()

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

app = Celery(
    'marketos_prod',
    broker=redis_url, # where tasks are send
    backend=redis_url, # where results are stored
    include=[
        'services.scraper_svc.scraper',
        'services.scraper_svc.extractor',
        'services.scraper_svc.semantics',
        'services.scraper_svc.celery_beat',
        'services.scraper_svc.candidate',
        'services.embedding_svc.main',
        'services.matcher_svc.main',
        'services.shopify_svc.main',
        'services.pricing_svc.main',
        'services.pricing_svc.stats',
        'services.pricing_svc.apply',
        'services.discovery_svc.main',
    ]
)

app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    worker_prefetch_multiplier=1,
    worker_redirect_stdouts=False,  # Fix for Prisma: avoids LoggingProxy fileno() error
    task_acks_late=True,            # Re-queue task if worker crashes mid-execution
    result_expires=3600,            # Auto-expire task results in Redis after 1 hour
    task_routes={
        'scraper.scrape_listing':                       {'queue': 'scraping_queue'},
        'scraper.rescrape_product':                     {'queue': 'scraping_queue'},
        'scraper.extract_product':                      {'queue': 'extraction_queue'},
        'scraper.rescrape_extract':                     {'queue': 'extraction_queue'},
        'scraper.generate_variant_semantics':           {'queue': 'semantic_queue'},
        'scraper.generate_shopify_variant_semantics':   {'queue': 'shopify_semantic_queue'},
        'embedder.generate_embeddings':                 {'queue': 'embedding_queue'},
        'shopify_embedder.generate_shopify_embeddings': {'queue': 'embedding_queue'},
        'matcher.match_for_scraped_product':            {'queue': 'match_queue'},
        'shopify_sync.recompute_sales_aggregate':       {'queue': 'shopify_sync_queue'},
        'shopify_sync.refresh_all_sales_aggregates':    {'queue': 'shopify_sync_queue'},
        'shopify_sync.pull_products':                   {'queue': 'shopify_sync_queue'},
        'stats.recompute_for_variant':                  {'queue': 'stats_queue'},
        'stats.recompute_after_observation':            {'queue': 'stats_queue'},
        'pricing.decide_for_product':                   {'queue': 'pricing_queue'},
        'pricing.apply_price':                          {'queue': 'pricing_queue'},
        'shopify_writer.apply_decision':                {'queue': 'writer_queue'},
        'shopify_writer.sweep_pending':                 {'queue': 'writer_queue'},
        'discovery.search_products':                    {'queue': 'discovery_queue'},
        'scraper.scrape_candidate':                     {'queue': 'scraping_queue'},
        'scraper.extract_candidate':                    {'queue': 'extraction_queue'},
        'scraper.expand_listing':                       {'queue': 'extraction_queue'},
        'scraper.rescrape_url':                         {'queue': 'scraping_queue'},
        'services.scraper_svc.celery_beat.check_idle_configs': {'queue': 'scheduler_queue'},
    },
    beat_schedule={
        'check-idle-configs-every-30-seconds': {
            'task': 'services.scraper_svc.celery_beat.check_idle_configs',
            'schedule': 30.0,
        },
        # NOTE: refresh_all_sales_aggregates and writer.sweep_pending were
        # removed when SalesAggregate / PricingRule were dropped in the
        # discovery_pivot migration. v1 pricing is suggestion-only (no
        # auto-apply), so the writer sweep has nothing to do.
    }
)

if __name__ == '__main__':
    app.start()
