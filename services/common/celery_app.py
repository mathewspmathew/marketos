import os
from celery import Celery
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
        'services.embedding_svc.main',
        'services.matcher_svc.main',
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
        'scraper.generate_shopify_variant_semantics':   {'queue': 'semantic_queue'},
        'embedder.generate_embeddings':                 {'queue': 'embedding_queue'},
        'shopify_embedder.generate_shopify_embeddings': {'queue': 'embedding_queue'},
        'matcher.match_for_shop':                       {'queue': 'match_queue'},
        'matcher.match_for_variant':                    {'queue': 'match_queue'},
        'services.scraper_svc.celery_beat.check_idle_configs': {'queue': 'scheduler_queue'},
        'services.scraper_svc.celery_beat.matcher_sweep':      {'queue': 'scheduler_queue'},
    },
    beat_schedule={
        'check-idle-configs-every-30-seconds': {
            'task': 'services.scraper_svc.celery_beat.check_idle_configs',
            'schedule': 30.0,
        },
        'matcher-sweep-nightly': {
            'task': 'services.scraper_svc.celery_beat.matcher_sweep',
            'schedule': 24 * 60 * 60.0,  # once per day
        },
    }
)

if __name__ == '__main__':
    app.start()
