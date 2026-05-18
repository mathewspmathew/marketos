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
from services.matcher_svc.scoring import (
    ABSOLUTE_HYBRID_FLOOR,
    CONFIRMED_THRESHOLD,
    LIKELY_THRESHOLD,
    PRICE_RATIO_LIMIT,
    brand_match,
    compute_confidence,
    confidence_tier,
)
from services.matcher_svc.threshold import compute_domain_threshold

load_dotenv()

_HNSW_EF_SEARCH = 100
_PER_DOMAIN_LIMIT = 50
_SHOP_LOCK_TTL_SECONDS = 30 * 60  # 30 minutes
_SUGGESTION_DEBOUNCE_SECONDS = 60  # one suggestion run per product per minute

# Hybrid scoring: re-rank text HNSW top-K by α·text_sim + (1-α)·img_sim when both
# vectors are available. Falls back to text-only if either side lacks vectorImg.
_HYBRID_TEXT_WEIGHT = 0.6

_redis_client: redis.Redis | None = None


def _redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
    return _redis_client

# nx mean Not eXists, ex means Expire time in seconds. So this command sets a key with a value and an expiration time, but only if the key does not already exist.
def _acquire_shop_lock(shop_domain: str) -> bool:
    return bool(_redis().set(f"match:lock:{shop_domain}", "1", nx=True, ex=_SHOP_LOCK_TTL_SECONDS))


def _release_shop_lock(shop_domain: str) -> None:
    _redis().delete(f"match:lock:{shop_domain}")


# ─────────────────────────────────────────────────────────────────────────────
# Per-variant matcher
# ─────────────────────────────────────────────────────────────────────────────

# takes ONE merchant variant - which competitor variants are similar enough to be considered matches? Writes ProductMatch rows.
def _match_variant(shop_domain: str, variant_id: str) -> int:
    """Returns the count of ProductMatch rows written/updated."""
    written = 0
    product_pair_scores: dict[tuple[str, str], list[float]] = {}

    with get_db() as session:
        # Load this variant's text + image vectors (string repr, casts back to vector in SQL).
        vec_row = session.execute(
            text(
                'SELECT "vectorText"::text AS vt, "vectorImg"::text AS vi '
                'FROM "ShopifyEmbedding" '
                'WHERE "variantId" = :vid AND "vectorText" IS NOT NULL'
            ),
            {"vid": variant_id},
        ).first()
        if not vec_row or not vec_row.vt:
            print(f"[matcher] no text vector for ShopifyVariant {variant_id[:8]} — skipping", flush=True)
            return 0
        query_text_vec = vec_row.vt
        query_img_vec = vec_row.vi  # may be None — falls back to text-only re-ranking

        # Merchant-side structured attributes used for SQL hard-filters and
        # confidence scoring. NULLs are tolerated — every filter degrades
        # permissively when either side lacks the field.
        attr_row = session.execute(
            text(
                'SELECT sp."id" AS prod_id, sp.vendor, sp."productType", '
                '       sp."categoryTop", sp."productGender", '
                '       sv."currentPrice" '
                'FROM "ShopifyVariant" sv '
                'JOIN "ShopifyProduct" sp ON sp.id = sv."productId" '
                'WHERE sv.id = :vid'
            ),
            {"vid": variant_id},
        ).first()
        merchant_prod_id   = attr_row.prod_id if attr_row else None
        merchant_vendor    = attr_row.vendor if attr_row else None
        merchant_type      = attr_row.productType if attr_row else None
        merchant_category  = attr_row.categoryTop if attr_row else None
        merchant_gender    = attr_row.productGender if attr_row else None
        merchant_price     = float(attr_row.currentPrice) if attr_row and attr_row.currentPrice else None
        
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
            # Text HNSW retrieves top-K candidates. The brand pre-filter applies
            # only when BOTH sides have a vendor — otherwise we'd reject every
            # competitor row from sources that don't extract vendor reliably.
            # Competitor variant attributes (vendor/type/price) ride along so
            # we can compute confidence without a second round-trip.
            rows = session.execute(
                text(
                    'SELECT pe."variantId" AS comp_variant_id, '
                    '       pe."prodId"    AS comp_prod_id, '
                    '       sp.vendor      AS comp_vendor, '
                    '       sp."productType" AS comp_type, '
                    '       sp."categoryTop" AS comp_category, '
                    '       sp."productGender" AS comp_gender, '
                    '       sv."currentPrice" AS comp_price, '
                    '       pe."vectorText" <=> CAST(:qvt AS vector) AS dist_text, '
                    '       CASE WHEN pe."vectorImg" IS NOT NULL AND CAST(:qvi AS text) IS NOT NULL '
                    '            THEN pe."vectorImg" <=> CAST(:qvi AS vector) '
                    '            ELSE NULL END AS dist_img '
                    'FROM "ProductEmbedding" pe '
                    'JOIN "ScrapedProduct" sp ON sp.id = pe."prodId" '
                    'JOIN "ScrapedVariant" sv ON sv.id = pe."variantId" '
                    'WHERE pe."shopDomain" = :sd '
                    '  AND sp.domain = :dom '
                    '  AND pe."variantId" IS NOT NULL '
                    # Brand pre-filter — only fires when BOTH sides have a vendor.
                    '  AND ( CAST(:mvendor AS text) IS NULL OR sp.vendor IS NULL '
                    '        OR LOWER(sp.vendor) = LOWER(CAST(:mvendor AS text)) ) '
                    # Top-level category hard filter — clothing vs electronics
                    # are never compared. Null on either side stays permissive.
                    '  AND ( CAST(:mcategory AS text) IS NULL OR sp."categoryTop" IS NULL '
                    '        OR sp."categoryTop" = CAST(:mcategory AS text) ) '
                    # Gender hard filter — men\'s product never matched to a
                    # women\'s product even if the rest of the signal aligns.
                    '  AND ( CAST(:mgender AS text) IS NULL OR sp."productGender" IS NULL '
                    '        OR sp."productGender" = CAST(:mgender AS text) ) '
                    # Price-ratio sanity — reject candidates whose latest price
                    # is >5x or <1/5x of the merchant variant. Permissive when
                    # either price is null (extractor may have failed).
                    '  AND ( CAST(:mprice AS numeric) IS NULL OR sv."currentPrice" IS NULL '
                    '        OR sv."currentPrice" BETWEEN CAST(:mprice AS numeric) / :ratio '
                    '                                  AND CAST(:mprice AS numeric) * :ratio ) '
                    # Suppress pairs the merchant has already rejected in review.
                    '  AND NOT EXISTS ( '
                    '        SELECT 1 FROM "ProductLevelMatch" plm '
                    '        WHERE plm."shopifyProductId" = :mprod '
                    '          AND plm."scrapedProductId" = pe."prodId" '
                    '          AND plm."rejectedByMerchant" = TRUE ) '
                    'ORDER BY pe."vectorText" <=> CAST(:qvt AS vector) '
                    f'LIMIT {_PER_DOMAIN_LIMIT}'
                ),
                {"qvt": query_text_vec, "qvi": query_img_vec,
                 "sd": shop_domain, "dom": domain,
                 "mvendor":   merchant_vendor,
                 "mcategory": merchant_category,
                 "mgender":   merchant_gender,
                 "mprice":    merchant_price,
                 "mprod":     merchant_prod_id,
                 "ratio":     PRICE_RATIO_LIMIT},
            ).all()
            if not rows:
                continue

            # Re-rank: when both sides have an image vector, score is
            # α·text_sim + (1-α)·img_sim; otherwise text-only.
            candidates = []
            for r in rows:
                text_sim = 1.0 - float(r.dist_text)
                if r.dist_img is not None:
                    img_sim = 1.0 - float(r.dist_img)
                    score = _HYBRID_TEXT_WEIGHT * text_sim + (1.0 - _HYBRID_TEXT_WEIGHT) * img_sim
                    mtype = "hybrid"
                else:
                    img_sim = None
                    score = text_sim
                    mtype = "semantic"
                candidates.append({
                    "comp_variant_id": r.comp_variant_id,
                    "comp_prod_id":    r.comp_prod_id,
                    "comp_vendor":     r.comp_vendor,
                    "comp_type":       r.comp_type,
                    "comp_price":      float(r.comp_price) if r.comp_price else None,
                    "dist_text":       float(r.dist_text),
                    "score":           score,
                    "mtype":           mtype,
                    "text_sim":        text_sim,
                    "img_sim":         img_sim,
                })

            # Absolute hybrid floor: ignore anything below this regardless of
            # what the domain-adaptive threshold would have allowed. Prevents
            # a domain with all-bad candidates from letting the best-bad through.
            candidates = [c for c in candidates if c["score"] >= ABSOLUTE_HYBRID_FLOOR]
            if not candidates:
                continue

            scores = [c["score"] for c in candidates]
            threshold = compute_domain_threshold(scores)
            kept = [c for c in candidates if c["score"] >= threshold]
            if not kept:
                continue

            # Top-1 per competitor product: a single competitor product with
            # N sibling variants would otherwise produce N near-identical
            # matches. We keep only the highest-scoring variant per product —
            # the one we are most confident corresponds to this merchant variant.
            best_by_product: dict[str, dict] = {}
            for c in kept:
                pid = c["comp_prod_id"]
                if pid not in best_by_product or c["score"] > best_by_product[pid]["score"]:
                    best_by_product[pid] = c
            kept = list(best_by_product.values())

            for c in kept:
                confidence = compute_confidence(
                    hybrid_sim=c["score"],
                    merchant_vendor=merchant_vendor,
                    competitor_vendor=c["comp_vendor"],
                    merchant_type=merchant_type,
                    competitor_type=c["comp_type"],
                    merchant_price=merchant_price,
                    competitor_price=c["comp_price"],
                )
                # Confidence may collapse to 0 from the hard price-ratio reject
                # in scoring.py. Skip the insert in that case.
                if confidence <= 0:
                    continue
                has_brand = brand_match(merchant_vendor, c["comp_vendor"])
                tier = confidence_tier(confidence, has_brand_match=has_brand)
                session.execute(
                    text(
                        'INSERT INTO "ProductMatch" '
                        '(id, "shopDomain", "shopifyVariantId", "competitorVariantId", '
                        ' "competitorProdId", "matchScore", "textScore", "imageScore", '
                        ' "matchType", "vectorDistance", "thresholdUsed", '
                        ' "confidence", "confidenceTier", '
                        ' "matchedAt", "updatedAt") '
                        'VALUES (:id, :sd, :svid, :cvid, :cpid, :score, :tscore, :iscore, '
                        '        :mtype, :dist, :thr, :conf, CAST(:tier AS "MatchConfidenceTier"), NOW(), NOW()) '
                        'ON CONFLICT ("shopifyVariantId", "competitorVariantId") DO UPDATE SET '
                        '  "competitorProdId" = EXCLUDED."competitorProdId", '
                        '  "matchScore"       = EXCLUDED."matchScore", '
                        '  "textScore"        = EXCLUDED."textScore", '
                        '  "imageScore"       = EXCLUDED."imageScore", '
                        '  "matchType"        = EXCLUDED."matchType", '
                        '  "vectorDistance"   = EXCLUDED."vectorDistance", '
                        '  "thresholdUsed"    = EXCLUDED."thresholdUsed", '
                        '  "confidence"       = EXCLUDED."confidence", '
                        '  "confidenceTier"   = EXCLUDED."confidenceTier", '
                        '  "updatedAt"        = NOW()'
                    ),
                    {
                        "id":     str(uuid.uuid4()),
                        "sd":     shop_domain,
                        "svid":   variant_id,
                        "cvid":   c["comp_variant_id"],
                        "cpid":   c["comp_prod_id"],
                        "score":  round(c["score"] * 100.0, 2),
                        "tscore": round(c["text_sim"] * 100.0, 2),
                        "iscore": round(c["img_sim"] * 100.0, 2) if c["img_sim"] is not None else None,
                        "mtype":  c["mtype"],
                        "dist":   round(c["dist_text"], 6),
                        "thr":    round(threshold, 4),
                        "conf":   round(confidence, 3),
                        "tier":   tier,
                    },
                )
                written += 1
                if merchant_prod_id:
                    product_pair_scores.setdefault(
                        (merchant_prod_id, c["comp_prod_id"]), []
                    ).append((confidence, has_brand))

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

        # Roll up variant matches to product-level matches. One ProductLevelMatch
        # per (merchantProduct, competitorProduct) pair, confidence = max of the
        # variant confidences in that pair. Max (vs mean) better reflects "the
        # best variant pairing is N% sure" rather than diluting with weaker
        # sibling variants.
        if product_pair_scores and merchant_prod_id:
            for (m_prod_id, c_prod_id), pairs in product_pair_scores.items():
                # Each entry is (confidence, has_brand). Pair confidence is
                # the max across variants; brand match is held if ANY variant
                # in the pair had it (a single brand-confirmed variant is
                # enough evidence that the product pair shares a brand).
                pair_conf = max(p[0] for p in pairs)
                pair_brand = any(p[1] for p in pairs)
                pair_tier = confidence_tier(pair_conf, has_brand_match=pair_brand)
                row = session.execute(
                    text(
                        'INSERT INTO "ProductLevelMatch" '
                        '(id, "shopDomain", "shopifyProductId", "scrapedProductId", '
                        ' "confidence", "confidenceTier", source, "confirmedByMerchant", '
                        ' "createdAt", "updatedAt") '
                        'VALUES (:id, :sd, :mpid, :cpid, :conf, CAST(:tier AS "MatchConfidenceTier"), '
                        '        :src, false, NOW(), NOW()) '
                        'ON CONFLICT ("shopifyProductId", "scrapedProductId") DO UPDATE SET '
                        '  confidence       = GREATEST("ProductLevelMatch".confidence, EXCLUDED.confidence), '
                        # Recomputed tier respects brand-match: only let the
                        # row reach CONFIRMED when this run carried brand
                        # evidence. If the prior tier was already CONFIRMED
                        # (merchant or earlier brand-evidence run), keep it.
                        '  "confidenceTier" = CASE '
                        '     WHEN "ProductLevelMatch"."confidenceTier" = '
                        '          CAST(\'CONFIRMED\' AS "MatchConfidenceTier") '
                        '       THEN CAST(\'CONFIRMED\' AS "MatchConfidenceTier") '
                        '     WHEN :brand_match = TRUE '
                        '       AND GREATEST("ProductLevelMatch".confidence, EXCLUDED.confidence) >= :confirmed '
                        '       THEN CAST(\'CONFIRMED\' AS "MatchConfidenceTier") '
                        '     WHEN GREATEST("ProductLevelMatch".confidence, EXCLUDED.confidence) >= :likely '
                        '       THEN CAST(\'LIKELY\' AS "MatchConfidenceTier") '
                        '     ELSE CAST(\'WEAK\' AS "MatchConfidenceTier") END, '
                        '  "updatedAt"      = NOW() '
                        'RETURNING id'
                    ),
                    {
                        "id":   str(uuid.uuid4()),
                        "sd":   shop_domain,
                        "mpid": m_prod_id,
                        "cpid": c_prod_id,
                        "conf": round(pair_conf, 3),
                        "tier": pair_tier,
                        "brand_match": pair_brand,
                        "src":  "RERANKER",
                        "confirmed": CONFIRMED_THRESHOLD,
                        "likely":    LIKELY_THRESHOLD,
                    },
                ).first()
                if row:
                    plm_id = row[0]
                    # Link every variant ProductMatch row in this pair to the
                    # product-level match so downstream consumers (pricing,
                    # confirmation UI) can navigate either direction.
                    session.execute(
                        text(
                            'UPDATE "ProductMatch" '
                            'SET "productMatchId" = :plm '
                            'WHERE "shopifyVariantId" = :svid '
                            '  AND "competitorProdId" = :cpid'
                        ),
                        {"plm": plm_id, "svid": variant_id, "cpid": c_prod_id},
                    )

        # Mark this merchant embedding clean — the dirty-flag selector uses
        # matchedAt vs. updatedAt to decide which variants need re-matching.
        session.execute(
            text(
                'UPDATE "ShopifyEmbedding" SET "matchedAt" = NOW() '
                'WHERE "variantId" = :vid'
            ),
            {"vid": variant_id},
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

    # Competitor-side dirtiness: any ProductEmbedding in this shop whose
    # current vector hasn't been consumed by the matcher yet. If even one
    # exists, every merchant variant in the shop needs a re-match because
    # the new competitor row could be a candidate for any of them.
    competitor_dirty = session.execute(
        text(
            'SELECT 1 FROM "ProductEmbedding" pe '
            'WHERE pe."shopDomain" = :sd '
            '  AND pe."vectorText" IS NOT NULL '
            '  AND (pe."matchedAt" IS NULL OR pe."matchedAt" < pe."vectorizedAt") '
            'LIMIT 1'
        ),
        {"sd": shop_domain},
    ).first() is not None

    if competitor_dirty:
        rows = session.execute(
            text(
                'SELECT se."variantId" '
                'FROM "ShopifyEmbedding" se '
                'WHERE se."shopDomain" = :sd AND se."vectorText" IS NOT NULL'
            ),
            {"sd": shop_domain},
        ).all()
        return [r[0] for r in rows]

    # Merchant-side dirtiness: embedding fresher than last match.
    rows = session.execute(
        text(
            'SELECT se."variantId" '
            'FROM "ShopifyEmbedding" se '
            'WHERE se."shopDomain" = :sd '
            '  AND se."vectorText" IS NOT NULL '
            '  AND (se."matchedAt" IS NULL OR se."matchedAt" < se."updatedAt")'
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
        written = _match_variant(shop_domain, shopify_variant_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            print(f"[matcher] variant {shopify_variant_id} permanently failed: {exc}", flush=True)
            return 0
        print(f"[matcher] variant {shopify_variant_id} failed: {exc} — retrying", flush=True)
        raise self.retry(exc=exc)

    # Fresh matches landed → recompute pricing stats for this variant; the
    # match set (and thus the weighted aggregates) changed.
    if written > 0:
        app.send_task(
            "stats.recompute_for_variant",
            args=[shop_domain, shopify_variant_id],
            queue="stats_queue",
        )

    # Also kick off a product-level suggestion run (existing content path).
    # Debounced per product so sibling variants matching back-to-back don't
    # fan out N parallel suggestion tasks for the same product.
    if written > 0:
        with get_db() as session:
            prod_row = session.execute(
                text('SELECT "productId" FROM "ShopifyVariant" WHERE id = :vid'),
                {"vid": shopify_variant_id},
            ).first()
        if prod_row and prod_row[0]:
            product_id = prod_row[0]
            lock_key = f"suggestion:debounce:{product_id}"
            if _redis().set(lock_key, "1", nx=True, ex=_SUGGESTION_DEBOUNCE_SECONDS):
                app.send_task(
                    "suggestion.suggest_for_product",
                    args=[shop_domain, product_id],
                    queue="suggestion_queue",
                )
                print(f"[matcher] queued suggestion for product {product_id[:8]}", flush=True)

    return written


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

        # Optimistically mark competitor embeddings clean now that variants
        # consuming them are queued. A PE write that lands after this stamp
        # bumps vectorizedAt above matchedAt and re-arms the dirty flag.
        with get_db() as session:
            session.execute(
                text(
                    'UPDATE "ProductEmbedding" SET "matchedAt" = NOW() '
                    'WHERE "shopDomain" = :sd AND "vectorText" IS NOT NULL'
                ),
                {"sd": shop_domain},
            )

        return len(variant_ids)
    finally:
        _release_shop_lock(shop_domain)
