"""
services/scraper_svc/main.py

Task 1 — scrape_listing  (scraping_queue)
  Discover product URLs from a competitor listing page (dynamic scroll for
  bot-protected sites, map_url + crawl_url fallback for open sites).
  Scrape each product page concurrently → upload .md to GCS.
  Persist discovered URLs to Redis for observability.
  Queue one extract_product task per product.

Task 2 — extract_product  (extraction_queue)
  Download .md from GCS → Groq LLM extraction → ProductSchema
  Upload product image to GCS → upsert ScrapedProduct + ScrapedVariant via ProductUrl
  Queue generate_variant_semantics task.

Task 3 — generate_variant_semantics  (extraction_queue)
  Fetch all variants for a product → ONE Groq call → rich semantic descriptions.
  Bulk-update ScrapedVariant.semanticText.
  Queue generate_embeddings task.
"""

import json
import math
import os
import random
import re as _re
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

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
from services.common.models import ProductUrl, ScrapedProduct, ScrapedVariant, ScrapingConfig
from services.common.schemas import ProductSchema

load_dotenv()

_groq_client      = Groq(api_key=os.getenv("GROQ_API_KEY", "not-set"))
_firecrawl_client = V1FirecrawlApp(api_key=os.getenv("FIRECRAWL_API_KEY", "not-set"))
_redis = redis_lib.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)

_PENDING_KEY_TTL    = 7200   # 2 h — Redis TTL for the in-flight extraction counter
_URLS_KEY_TTL       = 7200   # 2 h — Redis TTL for the discovered URL list
_MAX_SCRAPE_WORKERS = 3      # concurrent Firecrawl product-page requests


# ─────────────────────────────────────────────────────────────────────────────
# Groq prompt — product extraction
# ─────────────────────────────────────────────────────────────────────────────

GROQ_EXTRACT_PROMPT = """You are a professional e-commerce data extractor.
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


# ─────────────────────────────────────────────────────────────────────────────
# Groq prompt — variant semantic text generation
# ─────────────────────────────────────────────────────────────────────────────

GROQ_SEMANTIC_PROMPT = """You are an expert e-commerce copywriter specialising in semantic search optimisation.

Generate a rich, buyer-intent keyword description for EACH variant listed below.
This text powers a vector similarity search engine — when shoppers type queries like
"affordable red running shoe size 10 under 5000" or "wireless earbuds with noise cancellation",
your text must surface the right variant.

Product context:
  Name: {title}
  Brand: {vendor}
  Category: {product_type}
  Description: {description}
  Tags: {tags}
  Specifications: {specs}

Variants to describe (use the exact IDs as JSON keys):
{variants_json}

For each variant write exactly 2-3 sentences that:
1. Open with brand + product name + the defining option (colour, size, material, pack size, storage)
2. State price naturally — "priced at ₹X" or "on sale from ₹X down to ₹Y" — and availability
3. Weave in category, use-case, and 2-3 key specs buyers actually search for
4. Include synonyms and buyer vocabulary (e.g. "sneaker / trainer" not just "shoe")
5. Sound like a knowledgeable human, not a data dump

Return ONLY valid JSON: {{"<variant_id>": "<description>", ...}}
One key per variant ID provided. No markdown, no extra keys."""


# ─────────────────────────────────────────────────────────────────────────────
# Markdown cleaning
# ─────────────────────────────────────────────────────────────────────────────

_STRIP_PATTERNS = _re.compile(
    r'^(?:nav|navigation|menu|header|footer|breadcrumb|cookie|banner|sidebar'
    r'|skip to|©|\|.*\|.*\|)',
    _re.IGNORECASE | _re.MULTILINE,
)


def _clean_markdown(markdown: str) -> str:
    lines = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _STRIP_PATTERNS.match(stripped):
            continue
        if stripped.startswith('[') and stripped.endswith(')') and len(stripped) < 80:
            continue
        lines.append(line)
    return '\n'.join(lines)[:8000]


# ─────────────────────────────────────────────────────────────────────────────
# Groq helpers
# ─────────────────────────────────────────────────────────────────────────────

def extract_with_groq(markdown: str, url: str) -> ProductSchema | None:
    try:
        response = _groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Output JSON only."},
                {"role": "user",   "content": GROQ_EXTRACT_PROMPT.format(url=url) + f"\n\nMarkdown:\n{_clean_markdown(markdown)}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        raw  = response.choices[0].message.content
        data = json.loads(raw)
        if "product" in data and isinstance(data["product"], dict):
            return ProductSchema(**data["product"])
        return ProductSchema(**data)
    except GroqRateLimitError:
        raise
    except Exception as e:
        raw_preview = locals().get("raw", "")[:300]
        print(f"[!] Groq extraction error for {url}: {e}\n    Raw: {raw_preview}")
        return None


def _generate_semantic_texts(product: ScrapedProduct, variants: list) -> dict[str, str]:
    """One Groq call for all variants. Returns {variant_id: semantic_text}."""
    variants_json = json.dumps([
        {
            "id":             v.id,
            "title":          v.title,
            "options":        v.options or {},
            "current_price":  float(v.currentPrice or 0),
            "original_price": float(v.originalPrice) if v.originalPrice else None,
            "is_in_stock":    v.isInStock,
        }
        for v in variants
    ], ensure_ascii=False)

    prompt = GROQ_SEMANTIC_PROMPT.format(
        title=product.title,
        vendor=product.vendor or "Unknown Brand",
        product_type=product.productType or "Product",
        description=(product.description or "")[:500],
        tags=", ".join(product.tags) if isinstance(product.tags, list) else str(product.tags),
        specs=json.dumps(product.specifications or {}, ensure_ascii=False),
        variants_json=variants_json,
    )

    response = _groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": "Output JSON only."},
            {"role": "user",   "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    return json.loads(response.choices[0].message.content)


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
# DB upsert — checks ProductUrl first so the same URL updates the existing
# ScrapedProduct rather than inserting a duplicate.
# ─────────────────────────────────────────────────────────────────────────────

def upsert_to_db(
    config_id:  str,
    user_id:    str,
    url:        str,
    product:    ProductSchema,
    image_url:  str,
) -> str | None:
    domain = urlparse(url).netloc or "unknown"
    now    = datetime.now(timezone.utc)

    try:
        with get_db() as session:
            # ── 1. Resolve product_id via ProductUrl ──────────────────────────
            existing_url_row = session.query(ProductUrl).filter(ProductUrl.url == url).first()

            if existing_url_row:
                product_id = existing_url_row.prodId
                session.execute(
                    sa_update(ScrapedProduct)
                    .where(ScrapedProduct.id == product_id)
                    .values(
                        title=product.title,
                        description=product.description or "",
                        vendor=product.vendor or "",
                        productType=product.product_type or "",
                        tags=product.tags or [],
                        imageUrl=image_url,
                        specifications=json.loads(json.dumps(product.specifications)) if product.specifications else None,
                        updatedAt=now,
                    )
                )
                session.execute(
                    sa_update(ProductUrl)
                    .where(ProductUrl.id == existing_url_row.id)
                    .values(lastScrapedAt=now, status="ACTIVE", failCount=0)
                )
            else:
                product_id = str(uuid.uuid4())
                session.execute(
                    pg_insert(ScrapedProduct).values(
                        id=product_id,
                        userId=user_id,
                        domain=domain,
                        title=product.title,
                        description=product.description or "",
                        vendor=product.vendor or "",
                        productType=product.product_type or "",
                        tags=product.tags or [],
                        imageUrl=image_url,
                        specifications=json.loads(json.dumps(product.specifications)) if product.specifications else None,
                        updatedAt=now,
                    )
                )
                session.execute(
                    pg_insert(ProductUrl).values(
                        id=str(uuid.uuid4()),
                        userId=user_id,
                        configId=config_id,
                        prodId=product_id,
                        url=url,
                        status="ACTIVE",
                        failCount=0,
                        lastScrapedAt=now,
                    )
                )

            # ── 2. Replace variants (delete → bulk insert) ────────────────────
            session.query(ScrapedVariant).filter(ScrapedVariant.productId == product_id).delete()

            variants = product.variants or []
            if not variants:
                print(f"    [!] No variants for {product.title[:40]}")
                return product_id

            if len(variants) == 1:
                variants[0].title = product.title

            session.execute(
                pg_insert(ScrapedVariant),
                [
                    {
                        "id":            str(uuid.uuid4()),
                        "userId":        user_id,
                        "productId":     product_id,
                        "sku":           str(v.sku or ""),
                        "barcode":       v.barcode,
                        "title":         v.title,
                        "options":       v.options,
                        "currentPrice":  float(v.current_price or 0),
                        "originalPrice": float(v.original_price) if v.original_price else None,
                        "isInStock":     bool(v.is_in_stock),
                        "stockQuantity": v.stock_quantity,
                        "updatedAt":     now,
                    }
                    for v in variants
                ],
            )
            print(f"    [✓] DB saved: {product.title[:40]} | {len(variants)} variant(s)")
            return product_id

    except Exception as e:
        print(f"    [!] DB error for {url}: {e}\n{traceback.format_exc()}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# URL filtering
# ─────────────────────────────────────────────────────────────────────────────

_PRODUCT_ID_RE = _re.compile(r'/\d{6,10}(?:/|$|\?)')


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
    """Scrape one product page and upload markdown to GCS. Returns (url, gcs_ref) or None."""
    # Stagger concurrent requests so all workers don't hit the target simultaneously
    time.sleep(random.uniform(0.3, 1.5))
    try:
        result   = _firecrawl_client.scrape_url(
            product_url,
            formats=["markdown"],
            proxy=proxy,
            timeout=45000,
        )
        markdown = (
            result.get('markdown') if isinstance(result, dict)
            else getattr(result, 'markdown', None)
        ) or ""
    except Exception as e:
        print(f"    [!] Failed to scrape {product_url}: {e}", flush=True)
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
def scrape_listing(config_id: str, user_id: str, listing_url: str, num_products: int = 5):
    """Discover product URLs → scrape pages concurrently → queue extraction tasks."""
    _update_config_status(config_id, "RUNNING")

    domain      = urlparse(listing_url).netloc
    _use_stealth = any(d in domain for d in ('flipkart.com', 'amazon.', 'myntra.com'))
    _proxy       = 'stealth' if _use_stealth else None

    # ── URL Discovery ──────────────────────────────────────────────────────────
    print(f"[>] Discovering product URLs: {listing_url} (proxy={_proxy})", flush=True)
    raw_links  = []
    listing_md = ""

    if not _use_stealth:
        # map_url: fast sitemap-style URL dump, good for open sites
        try:
            map_result = _firecrawl_client.map_url(listing_url, params={"limit": num_products * 20})
            raw_links  = (
                map_result.get('links') if isinstance(map_result, dict)
                else getattr(map_result, 'links', None)
            ) or []
            print(f"[>] map_url returned {len(raw_links)} links", flush=True)
        except Exception as map_err:
            print(f"[!] map_url failed ({map_err}), falling back to scrape...", flush=True)

    if not raw_links:
        # For stealth sites (infinite-scroll grids) or map_url failure:
        # scroll the listing page to load lazily-rendered product cards.
        # Scroll count scales with the requested product count (~3 products per 1500px).
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
            _update_config_status(config_id, "IDLE")
            return

    # ── Mine URLs from rendered markdown (per-domain patterns) ─────────────────
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

    # ── Filter to product URLs ─────────────────────────────────────────────────
    seen: set[str] = set()
    product_urls   = []
    for u in raw_links:
        if isinstance(u, str) and u not in seen and is_product_url(u, listing_url):
            seen.add(u)
            product_urls.append(u)
            if len(product_urls) == num_products:
                break

    print(f"[>] {len(raw_links)} links → {len(product_urls)} product URLs after filter", flush=True)

    # ── crawl_url fallback for non-stealth when map_url yield was low ──────────
    if not _use_stealth and len(product_urls) < num_products:
        print(f"[>] Only {len(product_urls)}/{num_products} found — trying crawl_url fallback...", flush=True)
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
            # Re-filter with the extra links
            for u in raw_links:
                if isinstance(u, str) and u not in seen and is_product_url(u, listing_url):
                    seen.add(u)
                    product_urls.append(u)
                    if len(product_urls) == num_products:
                        break
            print(f"[>] After crawl_url fallback: {len(product_urls)} product URLs", flush=True)
        except Exception as crawl_err:
            print(f"[!] crawl_url fallback failed: {crawl_err}", flush=True)

    # Debug aid when nothing passes the filter
    if raw_links and not product_urls:
        id_links = [u for u in raw_links if _PRODUCT_ID_RE.search(urlparse(u).path)]
        print(f"[DEBUG] ID-pattern links: {id_links[:5]}", flush=True)

    if not product_urls:
        print("[!] No product URLs found — resetting to IDLE.", flush=True)
        _update_config_status(config_id, "IDLE")
        return

    # ── Persist discovered URLs to Redis (checkpoint before scraping) ──────────
    _redis.set(
        f"scrape_urls:{config_id}",
        json.dumps(product_urls),
        ex=_URLS_KEY_TTL,
    )
    print(f"[>] Saved {len(product_urls)} discovered URLs to Redis", flush=True)

    # ── Scrape product pages concurrently ──────────────────────────────────────
    print(f"[>] Scraping {len(product_urls)} product pages (workers={_MAX_SCRAPE_WORKERS})...", flush=True)
    uploaded_pages: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=_MAX_SCRAPE_WORKERS) as pool:
        futures = {
            pool.submit(_scrape_product, url, _proxy, domain): url
            for url in product_urls
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                uploaded_pages.append(result)

    n = len(uploaded_pages)
    print(f"[✓] scrape_listing done — {n} pages uploaded, queuing extraction.", flush=True)

    if not uploaded_pages:
        _update_config_status(config_id, "IDLE")
        return

    # Set Redis counter BEFORE queuing tasks to avoid a race where a fast task
    # completes and decrements before the counter is even initialised.
    _redis.set(f"scrape_pending:{config_id}", n, ex=_PENDING_KEY_TTL)

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
    try:
        counter_key = f"scrape_pending:{config_id}"
        remaining   = _redis.decr(counter_key)
        print(f"    [>] Pending counter for {config_id}: {remaining}", flush=True)
        if remaining <= 0:
            _redis.delete(counter_key)
            _update_config_status(config_id, "SCRAPED_FIRST")
            print(f"    [✓] Config {config_id} → SCRAPED_FIRST", flush=True)
    except Exception as e:
        print(f"    [!] Counter update failed for {config_id}: {e}", flush=True)


@app.task(name='scraper.extract_product', bind=True, max_retries=5, default_retry_delay=30, rate_limit='3/m')
def extract_product(self, config_id: str, user_id: str, product_url: str, gcs_ref: str):
    """Download .md → Groq extract → DB upsert via ProductUrl → queue semantic task."""
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
        print("    [!] Groq rate limited — retrying in 65s")
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

    prod_id = upsert_to_db(config_id, user_id, product_url, product, image_url)

    if prod_id:
        app.send_task(
            'scraper.generate_variant_semantics',
            args=[prod_id],
            queue='extraction_queue',
        )
        print(f"    [>] Queued semantic generation: {prod_id}")

    _mark_task_done(config_id)


# ─────────────────────────────────────────────────────────────────────────────
# Task 3: generate_variant_semantics
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name='scraper.generate_variant_semantics', bind=True, max_retries=3, default_retry_delay=30, rate_limit='3/m')
def generate_variant_semantics(self, product_id: str):
    """One Groq call generates semanticText for all variants, then queues embeddings."""
    print(f"[>] Generating semantic text for product {product_id}")

    try:
        with get_db() as session:
            product  = session.query(ScrapedProduct).filter(ScrapedProduct.id == product_id).first()
            variants = session.query(ScrapedVariant).filter(ScrapedVariant.productId == product_id).all()

            if not product:
                print(f"[!] Product {product_id} not found — skipping semantic generation")
                return
            if not variants:
                print(f"[!] No variants for product {product_id} — skipping")
                return

            try:
                semantic_map = _generate_semantic_texts(product, variants)
            except GroqRateLimitError:
                print("    [!] Groq rate limited — retrying in 65s")
                raise self.retry(countdown=65)
            except Exception as e:
                print(f"    [!] Groq semantic error: {e}")
                raise self.retry(exc=e)

            now = datetime.now(timezone.utc)
            updated = 0
            for v in variants:
                text = semantic_map.get(v.id, "")
                if text:
                    session.execute(
                        sa_update(ScrapedVariant)
                        .where(ScrapedVariant.id == v.id)
                        .values(semanticText=text, updatedAt=now)
                    )
                    updated += 1

            print(f"    [✓] semanticText written for {updated}/{len(variants)} variant(s) of '{product.title[:40]}'")

    except (GroqRateLimitError, self.MaxRetriesExceededError):
        raise
    except Exception as exc:
        raise self.retry(exc=exc)

    # Queue embedding after semantic text is committed
    app.send_task('embedder.generate_embeddings', args=[product_id], queue='embedding_queue')
    print(f"    [>] Queued embedding: {product_id}")
