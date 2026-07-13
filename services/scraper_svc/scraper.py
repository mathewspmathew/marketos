"""
services/scraper_svc/scraper.py

Task 1 — scrape_listing (scraping_queue)
  Discover product URLs → concurrent page scrapes → GCS upload.
  Persist discovered URL list to Redis (checkpoint).
  Queue one extract_product task per product.
"""

import json
import math
import os
import random
import re as _re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from billiard.exceptions import SoftTimeLimitExceeded
from dotenv import load_dotenv
from firecrawl import V1FirecrawlApp

from services.common.celery_app import app
from services.common.gcs_utils import upload_markdown_to_gcs
from services.scraper_svc.helpers import _redis, update_config_status, set_next_scrap_at, URLS_KEY_TTL, PENDING_KEY_TTL

import structlog

logger = structlog.get_logger(__name__)

load_dotenv()

_firecrawl_client = V1FirecrawlApp(api_key=os.getenv("FIRECRAWL_API_KEY", "not-set"))
_MAX_SCRAPE_WORKERS = 3
_PRODUCT_ID_RE = _re.compile(r'/\d{6,10}(?:/|$|\?)')


# ─────────────────────────────────────────────────────────────────────────────
# URL filtering
# ─────────────────────────────────────────────────────────────────────────────

def is_product_url(url: str, listing_url: str) -> bool:
    parsed      = urlparse(url)
    parsed_list = urlparse(listing_url)

    if parsed.path.rstrip("/") == parsed_list.path.rstrip("/"):
        return False
    if parsed.netloc and parsed_list.netloc and parsed.netloc != parsed_list.netloc:
        return False

    url_lower = url.lower()
    skip_patterns = [
        "/search?", "/search/", "/s?", "?q=", "?rawquery=", "searchterm=",
        "/category", "/browse", "/c/", "/wishlist", "/cart",
        "/login", "/signup", "/help", "/about", "/contact",
        "javascript:", "mailto:", "#",
    ]
    if any(p in url_lower for p in skip_patterns):
        return False

    if 'myntra.com' in url_lower:
        return bool(_PRODUCT_ID_RE.search(parsed.path))
    if 'amazon.' in url_lower:
        return '/dp/' in url_lower
    if 'flipkart.com' in url_lower:
        return '/p/' in url_lower

    segments = [s for s in parsed.path.split('/') if s]
    return len(segments) >= 2


# ─────────────────────────────────────────────────────────────────────────────
# Per-product scrape helper (runs inside ThreadPoolExecutor)
# ─────────────────────────────────────────────────────────────────────────────

def _scrape_product(product_url: str, proxy: str | None, domain: str) -> tuple[str, str] | None:
    time.sleep(random.uniform(0.3, 1.5))
    try:
        result   = _firecrawl_client.scrape_url(product_url, formats=["markdown"], proxy=proxy, timeout=45000)
        markdown = (
            result.get('markdown') if isinstance(result, dict)
            else getattr(result, 'markdown', None)
        ) or ""
    except Exception:
        logger.exception("firecrawl_scrape_failed", product_url=product_url, domain=domain)
        return None

    if not markdown or len(markdown.strip()) < 400:
        logger.warning("markdown_too_short", product_url=product_url, chars=len(markdown))
        return None

    gcs_ref = upload_markdown_to_gcs(markdown, domain, product_url)
    if not gcs_ref:
        logger.warning("gcs_upload_failed", product_url=product_url)
        return None

    logger.info("product_scraped", product_url=product_url, gcs_ref=gcs_ref)
    return product_url, gcs_ref


# ─────────────────────────────────────────────────────────────────────────────
# Task 1: scrape_listing
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name='scraper.scrape_listing', time_limit=600, soft_time_limit=540)
def scrape_listing(config_id: str, shop_domain: str, listing_url: str, num_products: int = 5):
    """Discover product URLs → concurrent scrape → queue extraction tasks."""
    try:
        _scrape_listing_inner(config_id, shop_domain, listing_url, num_products)
    except SoftTimeLimitExceeded:
        logger.warning("scrape_listing_soft_time_limit_hit", config_id=config_id)
        update_config_status(config_id, "IDLE")


def _scrape_listing_inner(config_id: str, shop_domain: str, listing_url: str, num_products: int):
    update_config_status(config_id, "RUNNING")

    domain       = urlparse(listing_url).netloc
    _use_stealth = any(d in domain for d in ('flipkart.com', 'amazon.', 'myntra.com'))
    _proxy       = 'stealth' if _use_stealth else None

    logger.info("discovering_urls", listing_url=listing_url, proxy=_proxy)
    raw_links  = []
    listing_md = ""

    if not _use_stealth:
        try:
            map_result = _firecrawl_client.map_url(listing_url, limit=num_products * 20)
            raw_links  = (
                map_result.get('links') if isinstance(map_result, dict)
                else getattr(map_result, 'links', None)
            ) or []
            logger.info("map_url_succeeded", link_count=len(raw_links))
        except Exception:
            logger.exception("map_url_failed", listing_url=listing_url)

    if not raw_links:
        n_scrolls = max(2, math.ceil(num_products / 3))
        _actions  = [{"type": "wait", "milliseconds": 3000}]
        for _ in range(n_scrolls):
            _actions += [
                {"type": "scroll", "direction": "down", "amount": 1500},
                {"type": "wait",   "milliseconds": 2000},
            ]
        try:
            listing_result = _firecrawl_client.scrape_url(
                listing_url,
                formats=['markdown', 'links'],
                proxy=_proxy,
                timeout=60000,
                actions=_actions if _use_stealth else None,
            )
            raw_links = (
                listing_result.get('links') if isinstance(listing_result, dict)
                else getattr(listing_result, 'links', None)
            ) or []
            listing_md = (
                listing_result.get('markdown') if isinstance(listing_result, dict)
                else getattr(listing_result, 'markdown', None)
            ) or ""
            logger.info("listing_scrape_succeeded", link_count=len(raw_links), markdown_chars=len(listing_md))
        except Exception:
            logger.exception("firecrawl_listing_scrape_failed", listing_url=listing_url)
            update_config_status(config_id, "IDLE")
            return

    # Per-domain URL mining from rendered markdown
    scheme = urlparse(listing_url).scheme
    base   = f"{scheme}://{urlparse(listing_url).netloc}"
    abs_md_links = _re.findall(r'https?://[^\s\)\]"\'<>]+', listing_md)

    if 'amazon.' in domain:
        rel_md_links = [base + r for r in _re.findall(r'/dp/[A-Z0-9]{10}', listing_md)]
    elif 'flipkart.com' in domain:
        rel_md_links = [base + r for r in _re.findall(r'/p/itm[a-zA-Z0-9]+', listing_md, _re.IGNORECASE)]
    elif 'myntra.com' in domain:
        rel_md_links = [base + r for r in _re.findall(
            r'(?<!\w)(/[a-zA-Z0-9][a-zA-Z0-9\-]*/[a-zA-Z0-9\-]+/[a-zA-Z0-9\-]+/\d{6,10}/\w+)',
            listing_md,
        )]
    else:
        rel_md_links = []

    if abs_md_links or rel_md_links:
        logger.info("mined_urls_from_markdown", abs_link_count=len(abs_md_links), rel_link_count=len(rel_md_links))

    raw_links = list(dict.fromkeys(raw_links + abs_md_links + rel_md_links))

    seen: set[str] = set()
    product_urls   = []
    for u in raw_links:
        if isinstance(u, str) and u not in seen and is_product_url(u, listing_url):
            seen.add(u)
            product_urls.append(u)
            if len(product_urls) == num_products:
                break

    logger.info("filtered_product_urls", raw_link_count=len(raw_links), product_url_count=len(product_urls))

    if not _use_stealth and len(product_urls) < num_products:
        logger.info("trying_crawl_url_fallback", found=len(product_urls), wanted=num_products)
        try:
            crawl_result = _firecrawl_client.crawl_url(
                listing_url,
                limit=num_products * 5,
                max_depth=1,
                poll_interval=3,
            )
            data = (
                crawl_result.get('data') if isinstance(crawl_result, dict)
                else getattr(crawl_result, 'data', None)
            ) or []
            crawl_links = []
            for page in data:
                page_links = (
                    page.get('links', []) if isinstance(page, dict)
                    else getattr(page, 'links', [])
                ) or []
                crawl_links.extend(page_links)
            raw_links = list(dict.fromkeys(raw_links + crawl_links))
            for u in raw_links:
                if isinstance(u, str) and u not in seen and is_product_url(u, listing_url):
                    seen.add(u)
                    product_urls.append(u)
                    if len(product_urls) == num_products:
                        break
            logger.info("crawl_url_fallback_succeeded", product_url_count=len(product_urls))
        except Exception:
            logger.exception("crawl_url_fallback_failed", listing_url=listing_url)

    if raw_links and not product_urls:
        id_links = [u for u in raw_links if _PRODUCT_ID_RE.search(urlparse(u).path)]
        logger.debug("id_pattern_links", sample=id_links[:5])

    if not product_urls:
        logger.warning("no_product_urls_found", listing_url=listing_url)
        update_config_status(config_id, "IDLE")
        return

    _redis.set(f"scrape_urls:{config_id}", json.dumps(product_urls), ex=URLS_KEY_TTL)

    logger.info("concurrent_scrape_starting", product_url_count=len(product_urls), workers=_MAX_SCRAPE_WORKERS)
    uploaded_pages: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=_MAX_SCRAPE_WORKERS) as pool:
        futures = {pool.submit(_scrape_product, url, _proxy, domain): url for url in product_urls}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    uploaded_pages.append(result)
            except Exception:
                logger.exception("scrape_thread_error", url=futures[future])

    n = len(uploaded_pages)
    logger.info("pages_uploaded", count=n)

    if not uploaded_pages:
        update_config_status(config_id, "IDLE")
        return

    # Set counter before sending so tasks that finish fast find the key.
    # Decrement for any send that fails so the counter stays accurate.
    _redis.set(f"scrape_pending:{config_id}", n, ex=PENDING_KEY_TTL)

    for product_url, gcs_ref in uploaded_pages:
        try:
            app.send_task(
                'scraper.extract_product',
                args=[config_id, shop_domain, product_url, gcs_ref],
                queue='extraction_queue',
            )
            logger.info("extraction_queued", product_url=product_url)
        except Exception:
            logger.exception("extraction_queue_failed", product_url=product_url)
            remaining = _redis.decr(f"scrape_pending:{config_id}")
            if remaining <= 0:
                _redis.delete(f"scrape_pending:{config_id}")
                update_config_status(config_id, "SCRAPED_FIRST")


# ─────────────────────────────────────────────────────────────────────────────
# Task: rescrape_product  (scraping_queue)
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name='scraper.rescrape_product', bind=True, max_retries=3, default_retry_delay=60)
def rescrape_product(self, config_id: str, shop_domain: str, product_url: str, prod_id: str):
    """Re-scrape a known product URL and queue extraction to update existing records."""
    domain      = urlparse(product_url).netloc
    _use_stealth = any(d in domain for d in ('flipkart.com', 'amazon.', 'myntra.com'))
    _proxy       = 'stealth' if _use_stealth else None

    logger.info("rescrape_starting", product_url=product_url)
    # Human-like delay before hitting the site — on top of the jitter inside _scrape_product
    time.sleep(random.uniform(2, 5))
    result = _scrape_product(product_url, _proxy, domain)
    if not result:
        if self.request.retries >= self.max_retries:
            logger.warning("rescrape_giving_up", product_url=product_url, max_retries=self.max_retries)
            set_next_scrap_at(config_id, product_url)
            return
        raise self.retry(exc=ValueError(f"Rescrape failed: {product_url}"))

    _, gcs_ref = result
    app.send_task(
        'scraper.rescrape_extract',
        args=[config_id, shop_domain, product_url, gcs_ref, prod_id],
        queue='extraction_queue',
    )
    logger.info("rescrape_extraction_queued", product_url=product_url)
