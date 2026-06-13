"""
services/scraper_svc/candidate.py

Candidate-driven scrape pipeline (kicked off by discovery_svc).

Tasks:
  scraper.scrape_candidate(candidate_id)
    Firecrawl a single product URL → upload markdown to GCS → enqueue extract.

  scraper.extract_candidate(candidate_id, gcs_ref)
    Groq extract → upsert ScrapedProduct + ScrapedVariants → create a
    product-rooted ProductUrl row (with shopifyProductId + frequency) →
    fan out to semantics + embedding workers. The semantics task fills
    ScrapedVariant.semanticText; embedder.generate_embeddings then writes
    ProductEmbedding.vectorText. ScrapedProduct↔ShopifyProduct confidence
    scoring (ProductLevelMatch) is the matcher_svc's job, not this file's.

This file does NOT touch the legacy listing-based scrape (scraper.scrape_listing).
That code path stays callable until the scheduler rewrite drops it.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv
from firecrawl import V1FirecrawlApp
from groq import RateLimitError as GroqRateLimitError
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from services.common.celery_app import app
from services.common.db import get_db
from services.common.gcs_utils import (
    download_markdown_from_gcs,
    upload_image_to_gcs,
    upload_markdown_to_gcs,
)
from services.common import models
from services.common.frequency import next_run_at as _freq_next_run_at
from services.scraper_svc.extractor import (
    _record_observations,
    extract_listing_with_groq,
    extract_with_groq,
    update_prices_in_db,
)
from services.scraper_svc.helpers import log_error
from services.scraper_svc.url_classifier import is_listing_url, registrable_domain

load_dotenv()

logger = logging.getLogger(__name__)

_firecrawl_client = V1FirecrawlApp(api_key=os.getenv("FIRECRAWL_API_KEY", "not-set"))

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────
MIN_MARKDOWN_LEN       = 400
FIRECRAWL_TIMEOUT_MS   = 45000

# Hard fallback cap when DiscoveryJob / ShopifyProduct / ShopSettings are all null.
DEFAULT_LISTING_EXPANSION_CAP = 5
# Per-card stagger when fanning out scrape_candidate from a listing expansion.
# Smooths Firecrawl + Vertex bursts; same-domain anyway.
LISTING_CARD_STAGGER_SECONDS  = 2
# Marker on CompetitorCandidate.source for rows born from listing expansion.
# Used as a recursion guard (parent.source == this  →  refuse to re-expand).
LISTING_EXPANSION_SOURCE = "listing_expansion"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _set_candidate_status(
    db, candidate_id: str, *, status: str | None = None, **fields
) -> None:
    """Convenience setter for CompetitorCandidate fields."""
    values: dict = dict(fields)
    if status is not None:
        values["status"] = status
    if not values:
        return
    db.execute(
        sa_update(models.CompetitorCandidate)
        .where(models.CompetitorCandidate.id == candidate_id)
        .values(**values)
    )


def _next_run_at(product: models.ShopifyProduct, settings: models.ShopSettings | None) -> datetime:
    """Resolve rescrape cadence: per-product override → shop default → daily."""
    unit     = product.frequencyUnit     or (settings.frequencyUnit     if settings else None)
    interval = product.frequencyInterval or (settings.frequencyInterval if settings else None)
    return _freq_next_run_at(interval, unit)


# ─────────────────────────────────────────────────────────────────────────────
# Task 1 — scrape_candidate
# ─────────────────────────────────────────────────────────────────────────────
@app.task(
    name="scraper.scrape_candidate",
    bind=True, max_retries=3, default_retry_delay=60,
    time_limit=180, soft_time_limit=150,
)
def scrape_candidate(self, candidate_id: str):
    """Firecrawl a single candidate URL → GCS → enqueue extract."""
    with get_db() as db:
        cand = db.get(models.CompetitorCandidate, candidate_id)
        if not cand:
            logger.warning("scrape_candidate: candidate %s missing", candidate_id)
            return {"status": "missing"}
        # Cooperative cancellation: if the merchant turned dynamic pricing OFF
        # for this product, abort before spending a Firecrawl call. The chatbot
        # disable flow flips dynamicPricingEnabled=false; this is the signal that
        # halts already-queued in-flight scrapes. (See the toggle-brief spec.)
        # shopifyProductId is NOT NULL; the None check below is strictly
        # defensive (e.g. a row deleted out from under us).
        product = db.get(models.ShopifyProduct, cand.shopifyProductId)
        if product is not None and not product.dynamicPricingEnabled:
            logger.info(
                "scrape_candidate: product %s dynamic pricing off, skipping %s",
                cand.shopifyProductId, candidate_id,
            )
            return {"status": "skipped_disabled"}
        url    = cand.url
        domain = cand.domain

    try:
        result   = _firecrawl_client.scrape_url(url, formats=["markdown"], timeout=FIRECRAWL_TIMEOUT_MS)
        markdown = (result.get("markdown") if isinstance(result, dict)
                    else getattr(result, "markdown", None)) or ""
    except Exception as exc:
        logger.warning("Firecrawl failed for %s: %s", url, exc)
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(
                    db, candidate_id,
                    status="DEAD", rejectReason=f"firecrawl_failed: {type(exc).__name__}",
                )
            return {"status": "dead"}
        raise self.retry(exc=exc)

    if len(markdown.strip()) < MIN_MARKDOWN_LEN:
        with get_db() as db:
            _set_candidate_status(
                db, candidate_id,
                status="REJECTED", rejectReason="markdown_too_short",
            )
        return {"status": "rejected_short_md"}

    gcs_ref = upload_markdown_to_gcs(markdown, domain, url)
    if not gcs_ref:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(
                    db, candidate_id,
                    status="DEAD", rejectReason="gcs_upload_failed",
                )
            return {"status": "dead"}
        raise self.retry(exc=RuntimeError("GCS upload failed"))

    # Listing/search-page URLs fan out into per-card scrapes instead of being
    # treated as a single product. The classifier is deterministic — when it
    # mis-fires, expand_listing simply marks the parent with zero cards and
    # the merchant only loses one Groq call.
    if is_listing_url(url):
        app.send_task(
            "scraper.expand_listing",
            args=[candidate_id, gcs_ref],
            queue="extraction_queue",
        )
        return {"status": "queued_listing_expand", "gcs_ref": gcs_ref}

    app.send_task(
        "scraper.extract_candidate",
        args=[candidate_id, gcs_ref],
        queue="extraction_queue",
    )
    return {"status": "queued_extract", "gcs_ref": gcs_ref}


# ─────────────────────────────────────────────────────────────────────────────
# Task 2 — extract_candidate
# ─────────────────────────────────────────────────────────────────────────────
@app.task(
    name="scraper.extract_candidate",
    bind=True, max_retries=5, default_retry_delay=30, rate_limit="3/m",
)
def extract_candidate(self, candidate_id: str, gcs_ref: str):
    """Groq extract → ScrapedProduct + product-rooted ProductUrl → fan out."""
    with get_db() as db:
        cand = db.get(models.CompetitorCandidate, candidate_id)
        if not cand:
            return {"status": "missing"}
        shop_domain        = cand.shopDomain
        url                = cand.url
        domain             = cand.domain
        shopify_product_id = cand.shopifyProductId

    markdown = download_markdown_from_gcs(gcs_ref)
    if not markdown:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="DEAD", rejectReason="gcs_empty")
            log_error(shop_domain, None, url, "GCS_EMPTY", "scraper.extract_candidate", gcs_ref, "")
            return {"status": "dead"}
        raise self.retry(exc=ValueError("empty markdown"))

    try:
        product = extract_with_groq(markdown, url)
    except GroqRateLimitError:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="DEAD", rejectReason="groq_rate_limited")
            return {"status": "dead"}
        raise self.retry(countdown=65)

    if not product or not product.title:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="REJECTED", rejectReason="groq_no_product")
            return {"status": "rejected_no_product"}
        raise self.retry(exc=ValueError("groq returned nothing"))

    image_url = ""
    if product.image_url and product.image_url.startswith("http"):
        image_url = upload_image_to_gcs(product.image_url) or ""

    # ── DB write ───────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    prod_id: str | None = None
    try:
        with get_db() as db:
            shopify_product = db.get(models.ShopifyProduct, shopify_product_id)
            settings        = db.get(models.ShopSettings, shop_domain)

            existing = (
                db.query(models.ProductUrl)
                .filter(models.ProductUrl.url == url)
                .first()
            )
            if existing:
                prod_id = existing.prodId
                db.execute(
                    sa_update(models.ScrapedProduct)
                    .where(models.ScrapedProduct.id == prod_id)
                    .values(
                        title=product.title,
                        description=product.description or "",
                        vendor=product.vendor or "",
                        productType=product.product_type or "",
                        tags=product.tags or [],
                        imageUrl=image_url,
                        specifications=(
                            json.loads(json.dumps(product.specifications))
                            if product.specifications else None
                        ),
                        updatedAt=now,
                    )
                )
            else:
                prod_id = str(uuid.uuid4())
                db.execute(
                    pg_insert(models.ScrapedProduct).values(
                        id=prod_id,
                        shopDomain=shop_domain,
                        domain=domain,
                        title=product.title,
                        description=product.description or "",
                        vendor=product.vendor or "",
                        productType=product.product_type or "",
                        tags=product.tags or [],
                        imageUrl=image_url,
                        specifications=(
                            json.loads(json.dumps(product.specifications))
                            if product.specifications else None
                        ),
                        updatedAt=now,
                    )
                )

            # Product-rooted URL row. configId is null (new flow).
            next_run = _next_run_at(shopify_product, settings) if shopify_product else None
            db.execute(
                pg_insert(models.ProductUrl)
                .values(
                    id=str(uuid.uuid4()),
                    shopDomain=shop_domain,
                    shopifyProductId=shopify_product_id,
                    configId=None,
                    prodId=prod_id,
                    url=url,
                    status="ACTIVE",
                    failCount=0,
                    frequencyInterval=(shopify_product.frequencyInterval if shopify_product else None),
                    frequencyUnit=(shopify_product.frequencyUnit if shopify_product else None),
                    lastScrapedAt=now,
                    nextRunAt=next_run,
                )
                .on_conflict_do_update(
                    index_elements=["url"],
                    set_={
                        "shopifyProductId": shopify_product_id,
                        "prodId":           prod_id,
                        "status":           "ACTIVE",
                        "failCount":        0,
                        "lastScrapedAt":    now,
                        "nextRunAt":        next_run,
                    },
                )
            )

            # Replace variants for this product.
            db.query(models.ScrapedVariant).filter(
                models.ScrapedVariant.productId == prod_id
            ).delete(synchronize_session=False)

            variants = product.variants or []
            if len(variants) == 1:
                variants[0].title = product.title

            variant_rows = [
                {
                    "id":            str(uuid.uuid4()),
                    "productId":     prod_id,
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
            ]
            if variant_rows:
                db.execute(pg_insert(models.ScrapedVariant), variant_rows)

                _record_observations(
                    db, shop_domain,
                    [
                        {
                            "competitorVariantId": r["id"],
                            "price":               r["currentPrice"],
                            "isInStock":           r["isInStock"],
                        }
                        for r in variant_rows
                        if r["currentPrice"] and r["currentPrice"] > 0
                    ],
                )

            _set_candidate_status(
                db, candidate_id,
                status="SCRAPED",
                scrapedProductId=prod_id, scrapedAt=now,
            )
    except Exception as exc:
        logger.exception("extract_candidate DB error for %s", url)
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="DEAD", rejectReason=f"db_error: {type(exc).__name__}")
            return {"status": "dead"}
        raise self.retry(exc=exc)

    # ── Fan out: variant semantics → embedding ─────────────────────────────
    # generate_variant_semantics fills ScrapedVariant.semanticText; then
    # embedder.generate_embeddings writes ProductEmbedding.vectorText.
    # Confidence scoring against the merchant's ShopifyProduct is the
    # matcher_svc's responsibility once the embeddings land.
    app.send_task(
        "scraper.generate_variant_semantics",
        args=[prod_id, None, shop_domain, url],
        queue="semantic_queue",
    )
    app.send_task("embedder.generate_embeddings", args=[prod_id], queue="embedding_queue")
    return {"status": "queued_embed", "scraped_product_id": prod_id}


# ─────────────────────────────────────────────────────────────────────────────
# Task 3 — expand_listing
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_listing_cap(db, cand: models.CompetitorCandidate) -> int:
    """DiscoveryJob → ShopifyProduct → ShopSettings → DEFAULT_LISTING_EXPANSION_CAP."""
    if cand.discoveryJobId:
        job = db.get(models.DiscoveryJob, cand.discoveryJobId)
        if job and job.listingExpansionCap is not None:
            return int(job.listingExpansionCap)
    product = db.get(models.ShopifyProduct, cand.shopifyProductId)
    if product and product.listingExpansionCap is not None:
        return int(product.listingExpansionCap)
    settings = db.get(models.ShopSettings, cand.shopDomain)
    if settings and settings.listingExpansionCap is not None:
        return int(settings.listingExpansionCap)
    return DEFAULT_LISTING_EXPANSION_CAP


@app.task(
    name="scraper.expand_listing",
    bind=True, max_retries=3, default_retry_delay=30, rate_limit="6/m",
)
def expand_listing(self, candidate_id: str, gcs_ref: str):
    """Fan a listing/search-page candidate into per-card child candidates.

    Pipeline:
      GCS markdown → Groq URL extract → same-domain filter → dedup
      → cap slice → insert child CompetitorCandidate rows
      → enqueue scrape_candidate per child (staggered)
      → mark parent SCRAPED with rejectReason='listing_expanded:N_cards'.

    Recursion guard: candidates whose source is already LISTING_EXPANSION are
    refused so we stay one level deep.
    """
    with get_db() as db:
        cand = db.get(models.CompetitorCandidate, candidate_id)
        if not cand:
            return {"status": "missing"}
        if cand.source == LISTING_EXPANSION_SOURCE:
            _set_candidate_status(db, candidate_id, status="REJECTED", rejectReason="listing_in_listing")
            return {"status": "rejected_recursive"}
        parent_url               = cand.url
        parent_domain            = cand.domain
        parent_reg_domain        = registrable_domain(parent_url)
        parent_shop_domain       = cand.shopDomain
        parent_shopify_product   = cand.shopifyProductId
        parent_discovery_job     = cand.discoveryJobId
        cap                      = _resolve_listing_cap(db, cand)

    markdown = download_markdown_from_gcs(gcs_ref)
    if not markdown:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="DEAD", rejectReason="gcs_empty")
            return {"status": "dead"}
        raise self.retry(exc=ValueError("empty markdown"))

    try:
        cards = extract_listing_with_groq(markdown, parent_url)
    except GroqRateLimitError:
        if self.request.retries >= self.max_retries:
            with get_db() as db:
                _set_candidate_status(db, candidate_id, status="DEAD", rejectReason="groq_rate_limited")
            return {"status": "dead"}
        raise self.retry(countdown=65)

    if not cards:
        with get_db() as db:
            _set_candidate_status(
                db, candidate_id,
                status="SCRAPED", rejectReason="listing_expanded:0_cards", scrapedAt=datetime.now(timezone.utc),
            )
        return {"status": "no_cards"}

    # Filter to same registrable domain (folds www/m subdomains) and drop the
    # parent listing URL itself. Dedup by URL.
    seen: set[str] = set()
    kept: list[dict] = []
    for c in cards:
        url = (c.get("url") or "").strip()
        if not url or url in seen:
            continue
        if url.rstrip("/") == parent_url.rstrip("/"):
            continue
        if registrable_domain(url) != parent_reg_domain:
            continue
        seen.add(url)
        kept.append(c)

    kept = kept[:cap]

    # Insert child candidates and enqueue staggered scrapes. Each child uses
    # source=LISTING_EXPANSION so the recursion guard above blocks any future
    # re-expansion. on_conflict_do_nothing on the (shopifyProductId, url)
    # unique constraint protects against duplicate inserts if the same
    # listing surfaces overlapping cards across runs.
    queued = 0
    now = datetime.now(timezone.utc)
    with get_db() as db:
        for i, c in enumerate(kept):
            child_id = str(uuid.uuid4())
            child_url = c["url"]
            child_domain = urlparse(child_url).netloc
            result = db.execute(
                pg_insert(models.CompetitorCandidate)
                .values(
                    id=child_id,
                    shopDomain=parent_shop_domain,
                    shopifyProductId=parent_shopify_product,
                    discoveryJobId=parent_discovery_job,
                    url=child_url,
                    domain=child_domain,
                    source=LISTING_EXPANSION_SOURCE,
                    serpTitle=c.get("title"),
                    status="PENDING",
                    discoveredAt=now,
                )
                .on_conflict_do_nothing(index_elements=["shopifyProductId", "url"])
                .returning(models.CompetitorCandidate.id)
            ).first()
            if not result:
                # URL already a candidate for this merchant product — skip.
                continue
            app.send_task(
                "scraper.scrape_candidate",
                args=[child_id],
                queue="scraping_queue",
                countdown=i * LISTING_CARD_STAGGER_SECONDS,
            )
            queued += 1

        _set_candidate_status(
            db, candidate_id,
            status="SCRAPED",
            rejectReason=f"listing_expanded:{queued}_cards",
            scrapedAt=now,
        )

    print(
        f"[expand_listing] parent={candidate_id[:8]} url={parent_url[:60]} "
        f"cards_seen={len(cards)} same_domain_kept={len(kept)} cap={cap} queued={queued}",
        flush=True,
    )
    return {"status": "expanded", "queued": queued}


# ─────────────────────────────────────────────────────────────────────────────
# Task 4 — rescrape_url (called by the scheduler tick)
# ─────────────────────────────────────────────────────────────────────────────
@app.task(
    name="scraper.rescrape_url",
    bind=True, max_retries=3, default_retry_delay=60,
    time_limit=180, soft_time_limit=150,
)
def rescrape_url(self, product_url_id: str):
    """Periodic refresh for a product-rooted ProductUrl.

    Light-weight relative to scrape_candidate: we only update variant prices/
    stock and record a new CompetitorPriceObservation. No re-verification, no
    new ProductLevelMatch — that gate was passed when the URL was created.
    """
    with get_db() as db:
        pu = db.get(models.ProductUrl, product_url_id)
        if not pu or pu.status != "ACTIVE":
            return {"status": "skipped"}
        url         = pu.url
        domain      = pu.domain if hasattr(pu, "domain") else urlparse(url).netloc
        prod_id     = pu.prodId
        shop_domain = pu.shopDomain
        shopify_product_id = pu.shopifyProductId
        # Pre-fetch the cadence inputs as primitives. Holding ORM instances
        # past the `with get_db()` block triggers DetachedInstanceError when
        # later code accesses lazy attributes.
        shopify_product = (
            db.get(models.ShopifyProduct, shopify_product_id)
            if shopify_product_id else None
        )
        settings = db.get(models.ShopSettings, shop_domain)
        freq_unit = (
            (shopify_product.frequencyUnit if shopify_product else None)
            or (settings.frequencyUnit if settings else None)
        )
        freq_interval = (
            (shopify_product.frequencyInterval if shopify_product else None)
            or (settings.frequencyInterval if settings else None)
        )
        has_schedule = shopify_product is not None

    def _compute_next_at():
        return _freq_next_run_at(freq_interval, freq_unit) if has_schedule else None

    def _reschedule_after_failure(status: str, increment_fail: bool = True):
        # Beat clears nextRunAt before dispatch; we must restamp it on every
        # exit path or the URL becomes a permanent dead row in the schedule.
        next_at = _compute_next_at()
        with get_db() as db:
            values: dict = {"nextRunAt": next_at}
            if increment_fail:
                values["failCount"] = models.ProductUrl.failCount + 1
            db.execute(
                sa_update(models.ProductUrl)
                .where(models.ProductUrl.id == product_url_id)
                .values(**values)
            )
        return {"status": status, "next_run_at": next_at.isoformat() if next_at else None}

    # ── Firecrawl ──────────────────────────────────────────────────────────
    try:
        result   = _firecrawl_client.scrape_url(url, formats=["markdown"], timeout=FIRECRAWL_TIMEOUT_MS)
        markdown = (result.get("markdown") if isinstance(result, dict)
                    else getattr(result, "markdown", None)) or ""
    except Exception as exc:
        logger.warning("rescrape Firecrawl failed for %s: %s", url, exc)
        if self.request.retries >= self.max_retries:
            return _reschedule_after_failure("fail")
        raise self.retry(exc=exc)

    if len(markdown.strip()) < MIN_MARKDOWN_LEN:
        return _reschedule_after_failure("fail_short_md")

    # Reuse the rescrape extraction path: Groq → variant price update.
    try:
        product = extract_with_groq(markdown, url)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            return _reschedule_after_failure("fail_extract")
        raise self.retry(exc=exc)

    if not product or not product.title:
        return _reschedule_after_failure("no_product")

    ok = update_prices_in_db(prod_id, url, product)
    if not ok:
        return _reschedule_after_failure("no_update")

    # Advance the schedule and clear failCount on success.
    next_at = _compute_next_at()
    with get_db() as db:
        db.execute(
            sa_update(models.ProductUrl)
            .where(models.ProductUrl.id == product_url_id)
            .values(
                lastScrapedAt=datetime.now(timezone.utc),
                nextRunAt=next_at,
                failCount=0,
            )
        )
    return {"status": "rescraped", "next_run_at": next_at.isoformat() if next_at else None}