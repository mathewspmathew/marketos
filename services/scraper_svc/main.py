"""
services/scraper_svc/main.py

Task 1 — scrape_listing  (scraping_queue)
  Firecrawl scrape listing page to get links
  Filter to actual product URLs (works for Zara, Myntra, Flipkart, Amazon, etc.)
  Scrape each product page individually for detailed markdown
  Upload .md to GCS (durable checkpoint)
  Queue one extract_product task per product

Task 2 — extract_product  (extraction_queue)
  Download .md from GCS
  Groq LLM extraction → ProductSchema
  Upload product image to GCS
  Upsert to PostgreSQL via SQLAlchemy
  Queue generate_embeddings task
"""

import json
import os
import re as _re
import time
import traceback
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse
#urlparse - divides the url to components - scheme, netloc, path, params, query, fragment

import redis as redis_lib
from dotenv import load_dotenv
from firecrawl import V1FirecrawlApp
from groq import Groq, RateLimitError as GroqRateLimitError
from sqlalchemy import func, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from services.common.celery_app import app
from services.common.db import get_db
from services.common.gcs_utils import (
    download_markdown_from_gcs,
    upload_image_to_gcs,
    upload_markdown_to_gcs,
)
from services.common.models import ScrapedProduct, ScrapedVariant, ScrapingConfig
from services.common.schemas import ProductSchema

load_dotenv()

_groq_client = Groq(api_key=os.getenv("GROQ_API_KEY", "not-set"))
# V1FirecrawlApp - specific version because - no break when firecrawl api updates 
_firecrawl_client = V1FirecrawlApp(api_key=os.getenv("FIRECRAWL_API_KEY", "not-set"))
_redis = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

# Redis key pattern: "scrape_pending:{config_id}" → int count of in-flight extraction tasks
_PENDING_KEY_TTL = 7200  # 2 hours


# ─────────────────────────────────────────────────────────────────────────────
# Groq extraction
# ─────────────────────────────────────────────────────────────────────────────

GROQ_PROMPT = """You are a professional e-commerce data extractor.
Extract structured product data from the markdown of this product page: {url}

Return a JSON object with a single 'product' key matching this schema:
  title: str
  description: str | null
  vendor: str | null  (brand/manufacturer)
  product_type: str | null  (e.g. 'Smartphone', 'Running Shoes')
  tags: list[str]  (max 5)
  image_url: str | null  (absolute http URL of the main product image)
  specifications: dict | null  (key specs as key-value pairs)
  variants: list of:
    title: str  (e.g. '128GB Black', 'Large Red', 'Pack of 2')
    current_price: float  (REQUIRED - real price from the page, never 0)
    original_price: float | null  (strike-through MRP if shown)
    is_in_stock: bool
    sku: str | null
    options: dict | null  (e.g. {{"Color": "Black", "Size": "UK 9"}})

RULES:
- variants MUST have at least 1 entry.
- If the product only has 1 option, use the product title as variant title with the real price.
- If multiple options exist, create one variant per option.
- Return ONLY raw JSON. No markdown. No commentary."""


_STRIP_PATTERNS = _re.compile(
    r'^(?:nav|navigation|menu|header|footer|breadcrumb|cookie|banner|sidebar'
    r'|skip to|©|\|.*\|.*\|)',
    _re.IGNORECASE | _re.MULTILINE,
)


def _clean_markdown(markdown: str) -> str:
    """Strip nav/footer noise and cap length to reduce Groq token usage."""
    lines = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _STRIP_PATTERNS.match(stripped):
            continue
        # drop pure-link lines (markdown nav menus)
        if stripped.startswith('[') and stripped.endswith(')') and len(stripped) < 80:
            continue
        lines.append(line)
    cleaned = '\n'.join(lines)
    return cleaned[:8000]


def extract_with_groq(markdown: str, url: str) -> ProductSchema | None:
    try:
        response = _groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Output JSON only."},
                {"role": "user",   "content": GROQ_PROMPT.format(url=url) + f"\n\nMarkdown:\n{_clean_markdown(markdown)}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        raw = response.choices[0].message.content
        data = json.loads(raw)
        if "product" in data and isinstance(data["product"], dict):
            return ProductSchema(**data["product"])
        return ProductSchema(**data)
    except GroqRateLimitError:
        raise
    except Exception as e:
        raw_preview = locals().get("raw", "")[:300]
        print(f"[!] Groq error for {url}: {e}\n    Raw: {raw_preview}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Config status helper
# ─────────────────────────────────────────────────────────────────────────────

def _update_config_status(config_id: str, status: str) -> None:
    with get_db() as session:
        session.execute(
            sa_update(ScrapingConfig)
            .where(ScrapingConfig.id == config_id)
            .values(status=status, updatedAt=func.now())
        )


# ─────────────────────────────────────────────────────────────────────────────
# DB upsert via SQLAlchemy
# ─────────────────────────────────────────────────────────────────────────────

def upsert_to_db(user_id: str, url: str, product: ProductSchema, image_url: str) -> str | None:
    """Upsert ScrapedProduct + ScrapedVariant rows. Returns the product DB id."""
    domain = urlparse(url).netloc or "unknown"
    semantic_text = (
        f"Product: {product.title} | Brand: {product.vendor} | "
        f"Category: {product.product_type} | {product.description or ''}"
    )

    now = datetime.now(timezone.utc)
    product_values = {
        "id":             str(uuid.uuid4()),
        "userId":         user_id,
        "url":            url,
        "domain":         domain,
        "title":          product.title,
        "description":    product.description or "",
        "vendor":         product.vendor or "",
        "productType":    product.product_type or "",
        "tags":           product.tags or [],
        "imageUrl":       image_url,
        "specifications": json.loads(json.dumps(product.specifications)) if product.specifications else None,
        "semanticText":   semantic_text,
        "vectorized":     False,
        "updatedAt":      now,   # @updatedAt has no DB DEFAULT; must be supplied explicitly
    }

    try:
        with get_db() as session:
            # INSERT ... ON CONFLICT (url) DO UPDATE — returns the surviving id
            stmt = (
                pg_insert(ScrapedProduct)
                .values(**product_values)
                .on_conflict_do_update(
                    index_elements=["url"],
                    set_={
                        **{k: pg_insert(ScrapedProduct).excluded[k]
                           for k in product_values if k not in ("id", "url")},
                        "updatedAt": func.now(),
                    },
                )
                .returning(ScrapedProduct.id)
            )
            product_id = session.execute(stmt).scalar_one()

            # Replace variants — delete then bulk-insert
            session.query(ScrapedVariant).filter(ScrapedVariant.productId == product_id).delete()

            variants = product.variants or []
            if not variants:
                print(f"    [!] No variants for {product.title[:40]}")
                return product_id

            if len(variants) == 1:
                variants[0].title = product.title

            variant_rows = [
                {
                    "id":                  str(uuid.uuid4()),
                    "userId":              user_id,
                    "productId":           product_id,
                    "sku":                 str(v.sku or ""),
                    "barcode":             v.barcode,
                    "title":               v.title,
                    "options":             v.options,
                    "currentPrice":        float(v.current_price or 0),
                    "originalPrice":       float(v.original_price) if v.original_price else None,
                    "isInStock":           bool(v.is_in_stock),
                    "stockQuantity":       v.stock_quantity,
                    "variantSemanticText": (
                        f"Variant of {product.title}: {v.title} | "
                        f"Price: {float(v.current_price or 0)} | "
                        f"Options: {json.dumps(v.options or {})}"
                    ),
                    "vectorized":  False,
                    "updatedAt":   now,  # @updatedAt has no DB DEFAULT
                }
                for v in variants
            ]
            session.execute(pg_insert(ScrapedVariant), variant_rows)

            print(f"    [✓] DB saved: {product.title[:40]} | {len(variants)} variant(s)")
            return product_id

    except Exception as e:
        print(f"    [!] DB error for {url}: {e}\n{traceback.format_exc()}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# URL filtering — works for Zara, Myntra, Flipkart, Amazon, generic sites
# ─────────────────────────────────────────────────────────────────────────────

_PRODUCT_ID_RE = _re.compile(r'/\d{6,10}(?:/|$|\?)')  # Myntra: /12345678/buy


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
# Task 1: scrape_listing
# ─────────────────────────────────────────────────────────────────────────────

#is task run more than 10 minutes - kill it immediately. in 9 min warn the task
@app.task(name='scraper.scrape_listing', time_limit=600, soft_time_limit=540)
def scrape_listing(config_id: str, user_id: str, listing_url: str, num_products: int = 5):
    """Scrape listing page → individual product pages → queue extraction tasks."""
    _update_config_status(config_id, "RUNNING")

    domain = urlparse(listing_url).netloc
    _use_stealth = any(d in domain for d in ('flipkart.com', 'amazon.', 'myntra.com'))
    _proxy = 'stealth' if _use_stealth else None

    # map_url doesn't support proxy — skip it for stealth domains and go straight
    # to scrape_url with proxy so bot-protected sites actually render.
    print(f"[>] Discovering product URLs: {listing_url} (proxy={_proxy})", flush=True)
    raw_links = []
    if not _use_stealth:
        try:
            #scraping works here - for non stealth sites
            map_result = _firecrawl_client.map_url(listing_url, params={"limit": num_products * 10})
            raw_links = (
                # is this is a dictionary - then get links using get method
                map_result.get('links')
                if isinstance(map_result, dict)
                # if it's an object - then use getattr to get links attribute
                else getattr(map_result, 'links', None)
                #nothing - empty list
            ) or []
            print(f"[>] map_url returned {len(raw_links)} links", flush=True)
        except Exception as map_err:
            print(f"[!] map_url failed ({map_err}), falling back to scrape...", flush=True)

    if not raw_links:
        # For stealth sites (Myntra/Flipkart/Amazon), product grids use infinite
        # scroll. Firecrawl sees only the nav shell without scroll actions.
        _actions = (
            [
                {"type": "wait",   "milliseconds": 3000},
                {"type": "scroll", "direction": "down", "amount": 1500},
                {"type": "wait",   "milliseconds": 2000},
                {"type": "scroll", "direction": "down", "amount": 1500},
                {"type": "wait",   "milliseconds": 2000},
            ]
            if _use_stealth else []
        )
        try:
            #scaping works here - stealth
            listing_result = _firecrawl_client.scrape_url(
                listing_url,
                formats=['markdown', 'links'],
                proxy=_proxy,
                timeout=60000,
                actions=_actions if _actions else None,
            )
            raw_links = (
                listing_result.get('links')
                if isinstance(listing_result, dict)
                else getattr(listing_result, 'links', None)
            ) or []
            
            #here md is taken from listing_result
            listing_md = (
                listing_result.get('markdown')
                if isinstance(listing_result, dict)
                else getattr(listing_result, 'markdown', None)
            ) or ""
            
            print(f"[>] scrape_url returned {len(raw_links)} links | md={len(listing_md)} chars", flush=True)
            print(f"[>] Markdown preview: {listing_md[:500]}", flush=True)
        except Exception as e:
            print(f"[!] Firecrawl listing scrape failed: {e}", flush=True)
            _update_config_status(config_id, "IDLE")
            return
    else:
        listing_md = ""

    # For JS-heavy sites, product links often don't appear in the links array
    # (they're rendered via click handlers). Mine the rendered markdown for
    # absolute and relative product URLs instead.
    scheme = urlparse(listing_url).scheme
    base   = f"{scheme}://{urlparse(listing_url).netloc}"
    abs_md_links = _re.findall(r'https?://[^\s\)\]"\'<>]+', listing_md)
    rel_md_links = [base + r for r in _re.findall(
        r'(?<!\w)(/[a-zA-Z0-9][a-zA-Z0-9\-]*/[a-zA-Z0-9\-]+/[a-zA-Z0-9\-]+/\d{6,10}/\w+)',
        listing_md,
    )]
    if abs_md_links or rel_md_links:
        print(f"[>] Mined {len(abs_md_links)} abs + {len(rel_md_links)} rel URLs from markdown", flush=True)
        #why dict keys - Because dict keys are unique 
    raw_links = list(dict.fromkeys(raw_links + abs_md_links + rel_md_links))

    # Deduplicate while preserving order before slicing
    seen: set[str] = set()
    product_urls = []
    for u in raw_links:
        if isinstance(u, str) and u not in seen and is_product_url(u, listing_url):
            seen.add(u)
            product_urls.append(u)
            if len(product_urls) == num_products:
                break

    print(f"[>] {len(raw_links)} links → {len(product_urls)} product URLs", flush=True)
    if raw_links and not product_urls:
        id_links = [u for u in raw_links if _PRODUCT_ID_RE.search(urlparse(u).path)]
        print(f"[DEBUG] ID-pattern links in full list: {id_links[:5]}", flush=True)
    if not product_urls:
        print("[!] No product URLs found — resetting to IDLE for retry.", flush=True)
        _update_config_status(config_id, "IDLE")
        return

# each product is extracted here - as .md file
    print(f"[>] Scraping {len(product_urls)} product pages...", flush=True)
    uploaded_pages = []

    for i, product_url in enumerate(product_urls):
        if i > 0:
            time.sleep(3)  # avoid triggering per-IP rate limits on target sites
        try:
            result = _firecrawl_client.scrape_url(
                product_url,
                formats=["markdown"],
                proxy=_proxy,
                timeout=45000,
            )
            markdown = (
                result.get('markdown')
                if isinstance(result, dict)
                else getattr(result, 'markdown', None)
            ) or ""
        except Exception as e:
            print(f"    [!] Failed to scrape {product_url}: {e}", flush=True)
            continue

        if not markdown or len(markdown.strip()) < 400:
            print(f"    [!] Markdown too short ({len(markdown)} chars) — skipping.", flush=True)
            continue
        
        #uploading that md to bucket 
        gcs_ref = upload_markdown_to_gcs(markdown, domain, product_url)
        if not gcs_ref:
            print(f"    [!] GCS upload failed — skipping.", flush=True)
            continue

        print(f"    [✓] Uploaded .md: {gcs_ref}", flush=True)
        uploaded_pages.append((product_url, gcs_ref))

    n = len(uploaded_pages)
    print(f"[✓] scrape_listing done. {n} extraction tasks queued.", flush=True)

    if not uploaded_pages:
        _update_config_status(config_id, "IDLE")
        return

    # Set the Redis counter BEFORE queuing tasks to avoid a race where a fast
    # task completes and decrements before the counter is even set.
    # eg: scrape_pending:123, 5, 2 hrs expire
    counter_key = f"scrape_pending:{config_id}"
    _redis.set(counter_key, n, ex=_PENDING_KEY_TTL)

    for product_url, gcs_ref in uploaded_pages:
        app.send_task(
            'scraper.extract_product',
            args=[config_id, user_id, product_url, gcs_ref],
            queue='extraction_queue',
        )
        print(f"    [✓] Queued extraction: {product_url[:70]}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Task 2: extract_product
# ─────────────────────────────────────────────────────────────────────────────

def _mark_task_done(config_id: str) -> None:
    """Decrement the Redis pending counter; mark config SCRAPED_FIRST when all tasks finish."""
    try:
        counter_key = f"scrape_pending:{config_id}"
        remaining = _redis.decr(counter_key)
        print(f"    [>] Pending counter for {config_id}: {remaining}", flush=True)
        if remaining <= 0:
            _redis.delete(counter_key)
            _update_config_status(config_id, "SCRAPED_FIRST")
            print(f"    [✓] Config {config_id} → SCRAPED_FIRST", flush=True)
    except Exception as e:
        print(f"    [!] Counter update failed for {config_id}: {e}", flush=True)


@app.task(name='scraper.extract_product', bind=True, max_retries=5, default_retry_delay=30, rate_limit='3/m')
def extract_product(self, config_id: str, user_id: str, product_url: str, gcs_ref: str):
    """Download .md from GCS → Groq extraction → image upload → DB upsert → queue embedding."""
    print(f"[>] Extracting: {product_url}")

    def give_up(reason: str) -> None:
        print(f"    [!] Giving up on {product_url[:60]}: {reason}")
        _mark_task_done(config_id)

    markdown = download_markdown_from_gcs(gcs_ref)
    if not markdown:
        if self.request.retries >= self.max_retries:
            give_up("empty markdown after max retries")
            return
        raise self.retry(exc=ValueError(f"Empty markdown from GCS: {gcs_ref}"))

    try:
        product = extract_with_groq(markdown, product_url)
    except GroqRateLimitError:
        print(f"    [!] Groq rate limited — retrying in 65s")
        if self.request.retries >= self.max_retries:
            give_up("Groq rate limited after max retries")
            return
        raise self.retry(countdown=65)

    if not product or not product.title:
        if self.request.retries >= self.max_retries:
            give_up("Groq returned no usable product")
            return
        raise self.retry(exc=ValueError(f"Groq returned nothing for {product_url}"))

    image_url = ""
    if product.image_url and product.image_url.startswith("http"):
        image_url = upload_image_to_gcs(product.image_url)
    else:
        print(f"    [-] No image for: {product.title[:40]}")

    prod_id = upsert_to_db(user_id, product_url, product, image_url)

    if prod_id:
        app.send_task('embedder.generate_embeddings', args=[prod_id], queue='embedding_queue')
        print(f"    [>] Queued embedding: {prod_id}")

    _mark_task_done(config_id)
