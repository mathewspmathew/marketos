"""
services/matcher_svc/main.py

Tasks (match_queue):
  matcher.match_for_shop(shop_domain, full=False)
    Find pending merchant variants for this shop and fan out per-variant
    match tasks. With full=True, re-matches every variant that has an
    embedding (used after a fresh competitor scrape, and by the nightly beat).

  matcher.match_for_variant(shop_domain, shopify_variant_id)
    For one merchant variant: per-domain HNSW similarity search across all
    competitor domains the shop tracks, hybrid threshold per domain, upsert
    surviving rows into ProductMatch and delete stale ones.

The matcher is read-mostly on ProductEmbedding/ShopifyEmbedding (HNSW idx) and
write-only against ProductMatch. pgvector types stay in raw SQL — SQLAlchemy
has no native vector type.
"""
import os
import uuid

import redis
from dotenv import load_dotenv
from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.matcher_svc.threshold import compute_domain_threshold

load_dotenv()

_HNSW_EF_SEARCH = 100
_PER_DOMAIN_LIMIT = 50
_SHOP_LOCK_TTL_SECONDS = 30 * 60  # 30 minutes

_redis_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
    return _redis_client


def _acquire_shop_lock(shop_domain: str) -> bool:
    return bool(_redis().set(f"match:lock:{shop_domain}", "1", nx=True, ex=_SHOP_LOCK_TTL_SECONDS))


def _release_shop_lock(shop_domain: str) -> None:
    _redis().delete(f"match:lock:{shop_domain}")


# ─────────────────────────────────────────────────────────────────────────────
# Per-variant matcher
# ─────────────────────────────────────────────────────────────────────────────

def _match_variant(shop_domain: str, variant_id: str) -> int:
    """Returns the count of ProductMatch rows written/updated."""
    written = 0

    with get_db() as session:
        # Load this variant's text vector once (string repr, casts back to vector in SQL).
        vec_row = session.execute(
            text(
                'SELECT "vectorText"::text AS v '
                'FROM "ShopifyEmbedding" '
                'WHERE "variantId" = :vid AND "vectorText" IS NOT NULL'
            ),
            {"vid": variant_id},
        ).first()
        if not vec_row or not vec_row.v:
            print(f"[matcher] no vector for ShopifyVariant {variant_id[:8]} — skipping", flush=True)
            return 0
        query_vec = vec_row.v

        # Distinct competitor domains tracked by this shop.
        domains = [
            r[0] for r in session.execute(
                text('SELECT DISTINCT domain FROM "ScrapedProduct" WHERE "shopDomain" = :sd'),
                {"sd": shop_domain},
            ).all()
        ]
        if not domains:
            print(f"[matcher] no competitor domains for {shop_domain} — skipping {variant_id[:8]}", flush=True)
            return 0

        # ef_search must be raised inside the transaction; SQLAlchemy session
        # holds an open tx, so SET LOCAL is correct here.
        session.execute(text(f"SET LOCAL hnsw.ef_search = {_HNSW_EF_SEARCH}"))

        for domain in domains:
            rows = session.execute(
                text(
                    'SELECT pe."variantId" AS comp_variant_id, '
                    '       pe."prodId"    AS comp_prod_id, '
                    '       pe."vectorText" <=> CAST(:qv AS vector) AS distance '
                    'FROM "ProductEmbedding" pe '
                    'JOIN "ScrapedProduct" sp ON sp.id = pe."prodId" '
                    'WHERE pe."shopDomain" = :sd '
                    '  AND sp.domain = :dom '
                    '  AND pe."variantId" IS NOT NULL '
                    'ORDER BY pe."vectorText" <=> CAST(:qv AS vector) '
                    f'LIMIT {_PER_DOMAIN_LIMIT}'
                ),
                {"qv": query_vec, "sd": shop_domain, "dom": domain},
            ).all()
            if not rows:
                continue

            candidates = [
                (r.comp_variant_id, r.comp_prod_id, float(r.distance), 1.0 - float(r.distance))
                for r in rows
            ]
            scores = [c[3] for c in candidates]
            threshold = compute_domain_threshold(scores)
            kept = [c for c in candidates if c[3] >= threshold]
            if not kept:
                continue

            for comp_variant_id, comp_prod_id, distance, similarity in kept:
                session.execute(
                    text(
                        'INSERT INTO "ProductMatch" '
                        '(id, "shopDomain", "shopifyVariantId", "competitorVariantId", '
                        ' "competitorProdId", "matchScore", "matchType", '
                        ' "vectorDistance", "thresholdUsed", "matchedAt", "updatedAt") '
                        'VALUES (:id, :sd, :svid, :cvid, :cpid, :score, \'semantic\', '
                        '        :dist, :thr, NOW(), NOW()) '
                        'ON CONFLICT ("shopifyVariantId", "competitorVariantId") DO UPDATE SET '
                        '  "competitorProdId" = EXCLUDED."competitorProdId", '
                        '  "matchScore"       = EXCLUDED."matchScore", '
                        '  "vectorDistance"   = EXCLUDED."vectorDistance", '
                        '  "thresholdUsed"    = EXCLUDED."thresholdUsed", '
                        '  "updatedAt"        = NOW()'
                    ),
                    {
                        "id":    str(uuid.uuid4()),
                        "sd":    shop_domain,
                        "svid":  variant_id,
                        "cvid":  comp_variant_id,
                        "cpid":  comp_prod_id,
                        "score": round(similarity * 100.0, 2),
                        "dist":  round(distance, 6),
                        "thr":   round(threshold, 4),
                    },
                )
                written += 1

        # Orphan cleanup only: rows whose competitor variant was deleted upstream
        # (FK SetNull) and weren't refreshed by this run. We deliberately keep
        # stale-but-valid rows so a single run that fails to write (empty
        # candidates, threshold rejection, transient HNSW miss) doesn't wipe
        # previously confirmed matches. Freshness is conveyed via updatedAt.
        session.execute(
            text(
                'DELETE FROM "ProductMatch" '
                'WHERE "shopifyVariantId" = :svid '
                '  AND "competitorVariantId" IS NULL'
            ),
            {"svid": variant_id},
        )

    print(f"[matcher] variant {variant_id[:8]} → {written} match row(s) ({len(domains)} domain(s))", flush=True)
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Per-shop dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def _select_pending_variants(session, shop_domain: str, full: bool) -> list[str]:
    if full:
        rows = session.execute(
            text(
                'SELECT se."variantId" '
                'FROM "ShopifyEmbedding" se '
                'WHERE se."shopDomain" = :sd AND se."vectorText" IS NOT NULL'
            ),
            {"sd": shop_domain},
        ).all()
        return [r[0] for r in rows]

    rows = session.execute(
        text(
            'SELECT v.id '
            'FROM "ShopifyVariant" v '
            'JOIN "ShopifyEmbedding" se ON se."variantId" = v.id '
            'LEFT JOIN "ProductMatch" pm ON pm."shopifyVariantId" = v.id '
            'WHERE se."shopDomain" = :sd AND se."vectorText" IS NOT NULL '
            'GROUP BY v.id, v."updatedAt" '
            'HAVING MAX(pm."matchedAt") IS NULL OR v."updatedAt" > MAX(pm."matchedAt")'
        ),
        {"sd": shop_domain},
    ).all()
    return [r[0] for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Celery tasks
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name="matcher.match_for_variant", bind=True, max_retries=3, default_retry_delay=60)
def match_for_variant(self, shop_domain: str, shopify_variant_id: str):
    try:
        return _match_variant(shop_domain, shopify_variant_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            print(f"[matcher] variant {shopify_variant_id} permanently failed: {exc}", flush=True)
            return 0
        print(f"[matcher] variant {shopify_variant_id} failed: {exc} — retrying", flush=True)
        raise self.retry(exc=exc)


@app.task(name="matcher.match_for_shop", bind=True)
def match_for_shop(self, shop_domain: str, full: bool = False):
    if not _acquire_shop_lock(shop_domain):
        print(f"[matcher] shop {shop_domain} already locked — skipping", flush=True)
        return 0

    try:
        with get_db() as session:
            variant_ids = _select_pending_variants(session, shop_domain, full)
        if not variant_ids:
            print(f"[matcher] shop {shop_domain}: no pending variants (full={full})", flush=True)
            return 0

        print(f"[matcher] shop {shop_domain}: queuing {len(variant_ids)} variant(s) (full={full})", flush=True)
        for vid in variant_ids:
            app.send_task(
                "matcher.match_for_variant",
                args=[shop_domain, vid],
                queue="match_queue",
            )
        return len(variant_ids)
    finally:
        _release_shop_lock(shop_domain)
