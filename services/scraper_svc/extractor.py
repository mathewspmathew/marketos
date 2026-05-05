"""
services/scraper_svc/extractor.py

Task 2 — extract_product (extraction_queue)
  Download .md from GCS → Groq extraction → upsert via ProductUrl.
  On permanent failure: log to ScrapingError table (DLQ).
  Queue generate_variant_semantics.
"""

import json
import os
import traceback
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv
from groq import Groq, RateLimitError as GroqRateLimitError
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from services.common.celery_app import app
from services.common.db import get_db
from services.common.gcs_utils import download_markdown_from_gcs, upload_image_to_gcs
from services.common.models import ProductUrl, ScrapedProduct, ScrapedVariant
from services.common.schemas import ProductSchema
from services.scraper_svc.helpers import log_error, mark_task_done, set_next_scrap_at

load_dotenv()

_groq_client = Groq(api_key=os.getenv("GROQ_API_KEY", "not-set"))

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

import re as _re

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


def upsert_to_db(
    config_id:   str,
    shop_domain: str,
    url:         str,
    product:     ProductSchema,
    image_url:   str,
) -> str | None:
    domain = urlparse(url).netloc or "unknown"
    now    = datetime.now(timezone.utc)

    try:
        with get_db() as session:
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
                    pg_insert(ProductUrl)
                    .values(
                        id=existing_url_row.id,
                        shopDomain=shop_domain,
                        configId=config_id,
                        prodId=product_id,
                        url=url,
                        status="ACTIVE",
                        failCount=0,
                        lastScrapedAt=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["url"],
                        set_={"lastScrapedAt": now, "status": "ACTIVE", "failCount": 0},
                    )
                )
            else:
                product_id = str(uuid.uuid4())
                session.execute(
                    pg_insert(ScrapedProduct).values(
                        id=product_id,
                        shopDomain=shop_domain,
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
                    pg_insert(ProductUrl)
                    .values(
                        id=str(uuid.uuid4()),
                        shopDomain=shop_domain,
                        configId=config_id,
                        prodId=product_id,
                        url=url,
                        status="ACTIVE",
                        failCount=0,
                        lastScrapedAt=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["url"],
                        set_={"lastScrapedAt": now, "status": "ACTIVE", "failCount": 0},
                    )
                )

            session.query(ScrapedVariant).filter(ScrapedVariant.productId == product_id).delete(synchronize_session=False)

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
# Targeted price/stock update (re-scrape path — no full upsert)
# ─────────────────────────────────────────────────────────────────────────────

def update_prices_in_db(
    prod_id:     str,
    product_url: str,
    product:     ProductSchema,
) -> bool:
    """Update only currentPrice / isInStock / stockQuantity on existing ScrapedVariants.
    Match extracted variants to existing rows by title (case-insensitive).
    Single-variant products match directly.
    Also stamps ProductUrl.lastScrapedAt.
    """
    now      = datetime.now(timezone.utc)
    extracted = product.variants or []
    if not extracted:
        print(f"    [!] No variants in re-scrape payload for {product_url[:60]}")
        return False

    try:
        with get_db() as session:
            existing = (
                session.query(ScrapedVariant)
                .filter(ScrapedVariant.productId == prod_id)
                .all()
            )
            if not existing:
                print(f"    [!] No existing variants for product {prod_id} — cannot update prices")
                return False

            updated = 0
            if len(existing) == 1:
                v_ex = extracted[0]
                session.execute(
                    sa_update(ScrapedVariant)
                    .where(ScrapedVariant.id == existing[0].id)
                    .values(
                        currentPrice  = float(v_ex.current_price or 0),
                        originalPrice = float(v_ex.original_price) if v_ex.original_price else None,
                        isInStock     = bool(v_ex.is_in_stock),
                        stockQuantity = v_ex.stock_quantity,
                        updatedAt     = now,
                    )
                )
                updated = 1
            else:
                by_title = {v.title.strip().lower(): v for v in existing}
                for v_ex in extracted:
                    key  = (v_ex.title or "").strip().lower()
                    v_db = by_title.get(key)
                    if not v_db:
                        continue
                    session.execute(
                        sa_update(ScrapedVariant)
                        .where(ScrapedVariant.id == v_db.id)
                        .values(
                            currentPrice  = float(v_ex.current_price or 0),
                            originalPrice = float(v_ex.original_price) if v_ex.original_price else None,
                            isInStock     = bool(v_ex.is_in_stock),
                            stockQuantity = v_ex.stock_quantity,
                            updatedAt     = now,
                        )
                    )
                    updated += 1

            session.execute(
                sa_update(ProductUrl)
                .where(ProductUrl.url == product_url)
                .values(lastScrapedAt=now)
            )

            print(f"    [✓] Price/stock updated: {updated}/{len(existing)} variant(s) for {product_url[:60]}")
            return updated > 0

    except Exception as e:
        print(f"    [!] Price update DB error for {product_url}: {e}\n{traceback.format_exc()}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Task 2b: rescrape_extract  (extraction_queue)
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name='scraper.rescrape_extract', bind=True, max_retries=3, default_retry_delay=30, rate_limit='3/m')
def rescrape_extract(self, config_id: str, shop_domain: str, product_url: str, gcs_ref: str, prod_id: str):
    """GCS download → Groq extract → targeted price/stock update → stamp timestamps."""
    print(f"[RescrapeExtract] {product_url[:70]}")

    def give_up(error_type: str, detail: str) -> None:
        print(f"    [!] Giving up rescrape extract {product_url[:60]}: {detail}")
        log_error(shop_domain, config_id, product_url, error_type, 'scraper.rescrape_extract', gcs_ref, detail)
        set_next_scrap_at(config_id, product_url)

    markdown = download_markdown_from_gcs(gcs_ref)
    if not markdown:
        if self.request.retries >= self.max_retries:
            give_up("GCS_EMPTY", "empty markdown after max retries")
            return
        raise self.retry(exc=ValueError(f"Empty markdown from GCS: {gcs_ref}"))

    try:
        product = extract_with_groq(markdown, product_url)
    except GroqRateLimitError:
        if self.request.retries >= self.max_retries:
            give_up("GROQ_RATE_LIMIT", "Groq rate limited after max retries")
            return
        raise self.retry(countdown=65)

    if not product or not product.title:
        if self.request.retries >= self.max_retries:
            give_up("GROQ_FAILED", "Groq returned nothing after max retries")
            return
        raise self.retry(exc=ValueError(f"Groq returned nothing for {product_url}"))

    ok = update_prices_in_db(prod_id, product_url, product)
    if not ok:
        if self.request.retries >= self.max_retries:
            give_up("DB_ERROR", "price/stock update failed after max retries")
            return
        raise self.retry(exc=RuntimeError(f"Price update failed for {product_url}"))

    set_next_scrap_at(config_id, product_url)


# ─────────────────────────────────────────────────────────────────────────────
# Task 2: extract_product
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name='scraper.extract_product', bind=True, max_retries=5, default_retry_delay=30, rate_limit='3/m')
def extract_product(self, config_id: str, shop_domain: str, product_url: str, gcs_ref: str):
    """Download .md → Groq extract → DB upsert → queue semantic generation."""
    print(f"[>] Extracting: {product_url}")

    def give_up(error_type: str, detail: str) -> None:
        print(f"    [!] Giving up on {product_url[:60]}: {detail}")
        log_error(shop_domain, config_id, product_url, error_type, 'scraper.extract_product', gcs_ref, detail)
        mark_task_done(config_id)

    markdown = download_markdown_from_gcs(gcs_ref)
    if not markdown:
        if self.request.retries >= self.max_retries:
            give_up("GCS_EMPTY", "empty markdown after max retries")
            return
        raise self.retry(exc=ValueError(f"Empty markdown from GCS: {gcs_ref}"))

    try:
        product = extract_with_groq(markdown, product_url)
    except GroqRateLimitError:
        print("    [!] Groq rate limited — retrying in 65s")
        if self.request.retries >= self.max_retries:
            give_up("GROQ_FAILED", "Groq rate limited after max retries")
            return
        raise self.retry(countdown=65)

    if not product or not product.title:
        if self.request.retries >= self.max_retries:
            give_up("GROQ_FAILED", "Groq returned no usable product after max retries")
            return
        raise self.retry(exc=ValueError(f"Groq returned nothing for {product_url}"))

    image_url = ""
    if product.image_url and product.image_url.startswith("http"):
        image_url = upload_image_to_gcs(product.image_url)
    else:
        print(f"    [-] No image for: {product.title[:40]}")

    prod_id = upsert_to_db(config_id, shop_domain, product_url, product, image_url)

    if not prod_id:
        if self.request.retries >= self.max_retries:
            give_up("DB_ERROR", "DB write failed after max retries")
            return
        raise self.retry(exc=RuntimeError(f"DB upsert failed for {product_url}"))

    set_next_scrap_at(config_id, product_url)
    app.send_task('scraper.generate_variant_semantics', args=[prod_id, config_id, shop_domain, product_url], queue='semantic_queue')
    print(f"    [>] Queued semantic generation: {prod_id}")
    mark_task_done(config_id)
