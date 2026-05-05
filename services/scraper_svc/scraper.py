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
    except Exception as e:
        print(f"    [!] Firecrawl failed for {product_url}: {e}", flush=True)
        return None

    if not markdown or len(markdown.strip()) < 400:
        print(f"    [!] Markdown too short ({len(markdown)} chars) — skipping {product_url[:60]}", flush=True)
        return None

    gcs_ref = upload_markdown_to_gcs(markdown, domain, product_url)
    if not gcs_ref:
        print(f"    [!] GCS upload failed — skipping {product_url[:60]}", flush=True)
        return None

    print(f"    [✓] Uploaded .md: {gcs_ref}", flush=True)
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
        print(f"[!] scrape_listing soft time limit hit for config {config_id} — resetting to IDLE", flush=True)
        update_config_status(config_id, "IDLE")


def _scrape_listing_inner(config_id: str, shop_domain: str, listing_url: str, num_products: int):
    update_config_status(config_id, "RUNNING")

    domain       = urlparse(listing_url).netloc
    _use_stealth = any(d in domain for d in ('flipkart.com', 'amazon.', 'myntra.com'))
    _proxy       = 'stealth' if _use_stealth else None

    print(f"[>] Discovering URLs: {listing_url} (proxy={_proxy})", flush=True)
    raw_links  = []
    listing_md = ""

    if not _use_stealth:
        try:
            map_result = _firecrawl_client.map_url(listing_url, params={"limit": num_products * 20})
            raw_links  = (
                map_result.get('links') if isinstance(map_result, dict)
                else getattr(map_result, 'links', None)
            ) or []
            print(f"[>] map_url returned {len(raw_links)} links", flush=True)
        except Exception as map_err:
            print(f"[!] map_url failed ({map_err})", flush=True)

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
            print(f"[>] scrape_url → {len(raw_links)} links | md={len(listing_md)} chars", flush=True)
        except Exception as e:
            print(f"[!] Firecrawl listing scrape failed: {e}", flush=True)
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
        print(f"[>] Mined {len(abs_md_links)} abs + {len(rel_md_links)} rel URLs from markdown", flush=True)

    raw_links = list(dict.fromkeys(raw_links + abs_md_links + rel_md_links))

    seen: set[str] = set()
    product_urls   = []
    for u in raw_links:
        if isinstance(u, str) and u not in seen and is_product_url(u, listing_url):
            seen.add(u)
            product_urls.append(u)
            if len(product_urls) == num_products:
                break

    print(f"[>] {len(raw_links)} links → {len(product_urls)} product URLs after filter", flush=True)

    if not _use_stealth and len(product_urls) < num_products:
        print(f"[>] Only {len(product_urls)}/{num_products} — trying crawl_url fallback...", flush=True)
        try:
            crawl_result = _firecrawl_client.crawl_url(
                listing_url,
                params={"limit": num_products * 5, "maxDepth": 1},
                wait_until_done=True,
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
            print(f"[>] After crawl_url fallback: {len(product_urls)} product URLs", flush=True)
        except Exception as crawl_err:
            print(f"[!] crawl_url fallback failed: {crawl_err}", flush=True)

    if raw_links and not product_urls:
        id_links = [u for u in raw_links if _PRODUCT_ID_RE.search(urlparse(u).path)]
        print(f"[DEBUG] ID-pattern links: {id_links[:5]}", flush=True)

    if not product_urls:
        print("[!] No product URLs found — resetting to IDLE.", flush=True)
        update_config_status(config_id, "IDLE")
        return

    _redis.set(f"scrape_urls:{config_id}", json.dumps(product_urls), ex=URLS_KEY_TTL)

    print(f"[>] Scraping {len(product_urls)} pages concurrently (workers={_MAX_SCRAPE_WORKERS})...", flush=True)
    uploaded_pages: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=_MAX_SCRAPE_WORKERS) as pool:
        futures = {pool.submit(_scrape_product, url, _proxy, domain): url for url in product_urls}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    uploaded_pages.append(result)
            except Exception as fut_err:
                print(f"    [!] Scrape thread error: {fut_err}", flush=True)

    n = len(uploaded_pages)
    print(f"[✓] {n} pages uploaded — queuing extraction.", flush=True)

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
            print(f"    [✓] Queued extraction: {product_url[:70]}", flush=True)
        except Exception as send_err:
            print(f"    [!] Failed to queue extraction for {product_url[:60]}: {send_err}", flush=True)
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

    print(f"[Rescrape] {product_url[:80]}", flush=True)
    # Human-like delay before hitting the site — on top of the jitter inside _scrape_product
    time.sleep(random.uniform(2, 5))
    result = _scrape_product(product_url, _proxy, domain)
    if not result:
        if self.request.retries >= self.max_retries:
            print(f"[Rescrape] Giving up on {product_url[:60]} after {self.max_retries} retries", flush=True)
            set_next_scrap_at(config_id, product_url)
            return
        raise self.retry(exc=ValueError(f"Rescrape failed: {product_url}"))

    _, gcs_ref = result
    app.send_task(
        'scraper.rescrape_extract',
        args=[config_id, shop_domain, product_url, gcs_ref, prod_id],
        queue='extraction_queue',
    )
    print(f"[Rescrape] Queued targeted extraction for {product_url[:70]}", flush=True)
