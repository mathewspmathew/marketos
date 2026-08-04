"""
services/discovery_svc/main.py

Competitor discovery — direct, single-query, no rerank.

Task (discovery_queue):
  discovery.search_products(shopify_product_id, query, num_results, job_id=None)

Pipeline:
  1. Load ShopifyProduct + ShopSettings (for marketplace blocklist).
  2. Hit Serper /search once with the merchant-supplied query.
  3. Domain blocklist: drop own shop, social/content sites, marketplaces from
     settings. Cap N per domain. Trust the query for relevance.
  4. Upsert top `num_results` survivors as CompetitorCandidate (PENDING).
  5. Enqueue scraper.scrape_candidate(candidate_id) for each.
  6. Mark DiscoveryJob COMPLETED with candidate count.

Verification still happens later in scraper_svc after extract.
"""
from __future__ import annotations

import structlog
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from services.common.celery_app import app
from services.common.db import get_db
from services.common import models
from services.discovery_svc import serper

logger = structlog.get_logger(__name__)

MAX_PER_DOMAIN = 2
CONTENT_URL_BLOCKLIST = (
    "/blog/", "/blogs/", "/review/", "/reviews/", "/news/", "/wiki/", "/article/",
)
HOST_BLOCKLIST = (
    "youtube.com", "youtu.be", "reddit.com", "pinterest.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com", "quora.com", "medium.com",
    "google.com", "wikipedia.org",
)


# Query params used purely for tracking/attribution — stripping them lets the
# (shopifyProductId, url) unique constraint dedup the same product link served
# by Serper with rotating srsltid/utm tokens.
_TRACKING_QUERY_KEYS = {
    "srsltid", "fbclid", "gclid", "msclkid", "yclid", "ref", "ref_src",
    "_gl", "mc_cid", "mc_eid", "igshid",
}
_TRACKING_QUERY_PREFIXES = ("utm_",)


def _normalize_url(url: str) -> str:
    """Strip tracking query params and fragment; lower-case the host."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        kept = [
            (k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True)
            if k not in _TRACKING_QUERY_KEYS
            and not any(k.startswith(p) for p in _TRACKING_QUERY_PREFIXES)
        ]
        return urlunparse((
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            urlencode(kept, doseq=True),
            "",
        ))
    except Exception:
        logger.debug("url_normalize_failed", url=url)
        return url


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        logger.debug("domain_parse_failed", url=url)
        return ""


def _looks_like_content(url: str) -> bool:
    lower = url.lower()
    return any(token in lower for token in CONTENT_URL_BLOCKLIST)


def _filter_candidates(
    hits: list[serper.CandidateHit],
    *,
    shop_domain: str,
    marketplace_blocklist: list[str],
    limit: int,
) -> list[serper.CandidateHit]:
    seen_urls: set[str] = set()
    per_domain: dict[str, int] = {}
    out: list[serper.CandidateHit] = []

    host_blocked = set(HOST_BLOCKLIST) | set(marketplace_blocklist)
    shop_host = shop_domain.lower()

    for h in hits:
        if not h.url:
            continue
        h.url = _normalize_url(h.url)
        if h.url in seen_urls:
            continue
        dom = h.domain or _domain_of(h.url)
        if not dom:
            continue
        if dom == shop_host or dom.endswith(f".{shop_host}"):
            continue
        if any(dom == b or dom.endswith(f".{b}") for b in host_blocked):
            continue
        if _looks_like_content(h.url):
            continue
        if per_domain.get(dom, 0) >= MAX_PER_DOMAIN:
            continue
        seen_urls.add(h.url)
        per_domain[dom] = per_domain.get(dom, 0) + 1
        out.append(h)
        if len(out) >= limit:
            break

    return out


@app.task(name="discovery.search_products", bind=True, max_retries=2, default_retry_delay=30)
def search_products(
    self,
    shopify_product_id: str,
    query: str,
    num_results: int = 10,
    job_id: str | None = None,
):
    """Run a one-shot discovery search for a merchant product."""
    saved_ids: list[str] = []
    job_id_out: str | None = None

    query = (query or "").strip()
    if not query:
        return {"status": "missing_query"}
    num_results = max(1, min(int(num_results or 10), 50))

    # READ PHASE (short txn)
    with get_db() as db:
        product = db.get(models.ShopifyProduct, shopify_product_id)
        if not product:
            logger.warning("product_not_found", shopify_product_id=shopify_product_id)
            return {"status": "missing_product"}

        settings  = db.get(models.ShopSettings, product.shopDomain)
        blocklist = list(settings.marketplaceBlocklist) if settings else []
        serper_gl       = (settings.serperGl       if settings else None) or "in"
        serper_hl       = (settings.serperHl       if settings else None) or "en"
        serper_location = (settings.serperLocation if settings else None) or "Kochi, Kerala"

        job = db.get(models.DiscoveryJob, job_id) if job_id else None
        if job is None:
            job = models.DiscoveryJob(
                shopDomain=product.shopDomain,
                shopifyProductId=product.id,
                status="RUNNING",
                query=query,
            )
            db.add(job)
            db.flush()
        else:
            job.status     = "RUNNING"
            job.query      = query
        db.flush()

        product_id  = product.id
        shop_domain = product.shopDomain
        job_id_local    = job.id

    # SEARCH PHASE (no DB held) — Serper HTTP call has unbounded latency, so the
    # DB session above is closed before we make it.
    try:
        raw_hits = serper.search_web(
            query,
            num=num_results,
            gl=serper_gl,
            hl=serper_hl,
            location=serper_location,
        )
        filtered = _filter_candidates(
            raw_hits,
            shop_domain=shop_domain,
            marketplace_blocklist=blocklist,
            limit=num_results,
        )
        logger.info(
            "search_products_query_results",
            product_id=product_id,
            query=query,
            raw_hits=len(raw_hits),
            kept_hits=len(filtered),
        )
    except Exception as exc:
        # WRITE PHASE — failure (short txn)
        error = str(exc)[:1000]
        with get_db() as db:
            db.execute(
                update(models.DiscoveryJob)
                .where(models.DiscoveryJob.id == job_id_local)
                .values(status="FAILED", error=error, completedAt=datetime.now(timezone.utc))
            )
        logger.exception(
            "search_products_failed",
            shopify_product_id=shopify_product_id,
            job_id=job_id_local,
        )
        if self.request.retries >= self.max_retries:
            return {"status": "failed", "job_id": job_id_local, "error": error}
        # Pass job_id forward explicitly so the retry reuses this same
        # DiscoveryJob row instead of creating a new one (job_id is None
        # on a fresh job's original call — a plain self.retry() would
        # resend the original args and hit the "create new job" branch
        # above again on every attempt).
        raise self.retry(
            exc=exc,
            kwargs={
                "shopify_product_id": shopify_product_id,
                "query": query,
                "num_results": num_results,
                "job_id": job_id_local,
            },
        )

    # WRITE PHASE — success (short txn)
    now = datetime.now(timezone.utc)
    with get_db() as db:
        for hit in filtered:
            stmt = pg_insert(models.CompetitorCandidate.__table__).values(
                shopDomain       = shop_domain,
                shopifyProductId = product_id,
                discoveryJobId   = job_id_local,
                url              = hit.url,
                domain           = hit.domain,
                source           = hit.source,
                serpTitle        = hit.title,
                serpSnippet      = hit.snippet,
                serpPrice        = hit.price,
                status           = "PENDING",
                discoveredAt     = now,
            ).on_conflict_do_update(
                index_elements=["shopifyProductId", "url"],
                set_={
                    "discoveryJobId": job_id_local,
                    "serpTitle":      hit.title,
                    "serpSnippet":    hit.snippet,
                    "serpPrice":      hit.price,
                },
            ).returning(models.CompetitorCandidate.id)
            saved_ids.append(db.execute(stmt).scalar_one())

        db.execute(
            update(models.DiscoveryJob)
            .where(models.DiscoveryJob.id == job_id_local)
            .values(status="COMPLETED", completedAt=now)
        )
        db.execute(
            update(models.ShopifyProduct)
            .where(models.ShopifyProduct.id == product_id)
            .values(lastDiscoveryAt=now)
        )
        job_id_out = job_id_local

    for cid in saved_ids:
        try:
            app.send_task("scraper.scrape_candidate", args=[cid], queue="scraping_queue")
        except Exception:
            logger.exception("scrape_candidate_dispatch_failed", candidate_id=cid)

    return {"job_id": job_id_out, "candidates": len(saved_ids)}
