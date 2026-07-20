"""
services/matcher_svc/main.py

Single task (match_queue):
  matcher.match_for_scraped_product(scraped_product_id)
    Triggered when a ScrapedProduct's embeddings finish writing. Resolves the
    merchant product(s) that caused the scrape via CompetitorCandidate, then
    computes scoped pairwise cosine between each merchant variant's
    ShopifyEmbedding and the scraped variants' ProductEmbeddings. Writes/
    updates ProductMatch + one ProductLevelMatch per (merchantProduct,
    scrapedProduct) pair. Fans out stats tasks for downstream pricing engine.

Design:
  - Scope is the scraped product, not the shop. No HNSW, no per-domain
    threshold — the candidate set is tiny (N merchant variants × M scraped
    variants for one product pair).
  - Hard filters: gender, rejectedByMerchant guard. Vector similarity +
    threshold do the primary discriminating.
  - Category and brand are intentionally NOT hard-gated. Both are free-form
    strings (LLM-assigned category; scraped vendor), so the same product is
    routinely labelled differently on the merchant vs. scraped side — e.g.
    category "home" vs "household", or vendor "Fevicol" vs "Pidilite" vs
    "PEDILITE" for the identical glue. An exact-equality hard gate silently
    dropped valid matches, and structurally guaranteed zero matches for
    private-label merchants (own brand never equals a competitor's brand).
    Brand still feeds compute_confidence (bonus) and the CONFIRMED tier
    requirement in scoring.py, so it survives as a soft signal.
  # NOTE: As of the per-product scrape isolation fix (2026-07-13), new scrapes
  # never let two different shopifyProductIds share one ScrapedProduct row —
  # services/scraper_svc/candidate.py::_upsert_scraped_product dedups per
  # (shopifyProductId, url), not globally by url. Older rows written before
  # that fix may still have multiple CompetitorCandidate rows pointing at one
  # ScrapedProduct (no backfill was performed), so this loop still needs to
  # handle that case correctly for legacy data — it just should no longer
  # happen for anything scraped going forward.
"""
import uuid

import structlog
from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.matcher_svc.scoring import (
    ABSOLUTE_HYBRID_FLOOR,
    CONFIRMED_THRESHOLD,
    LIKELY_THRESHOLD,
    brand_match,
    compute_confidence,
    confidence_tier,
)

logger = structlog.get_logger(__name__)

# Hybrid scoring: α·text_sim + (1-α)·img_sim when both vectors are available.
_HYBRID_TEXT_WEIGHT = 0.6


# ─────────────────────────────────────────────────────────────────────────────
# Scoped pairwise matcher
# ─────────────────────────────────────────────────────────────────────────────

def _match_one_pair(
    session,
    shop_domain: str,
    merchant_product_id: str,
    scraped_product_id: str,
) -> tuple[int, set[str]]:
    """Compute matches for one (merchantProduct, scrapedProduct) pair.

    Returns (rows_written, set_of_merchant_variant_ids_touched).
    """
    written = 0
    touched_variants: set[str] = set()

    # If the merchant has already rejected this pair in review, skip entirely.
    already_rejected = session.execute(
        text(
            'SELECT 1 FROM "ProductLevelMatch" '
            'WHERE "shopifyProductId" = :mpid '
            '  AND "scrapedProductId" = :cpid '
            '  AND "reviewStatus" = \'REJECTED\''
        ),
        {"mpid": merchant_product_id, "cpid": scraped_product_id},
    ).first()
    if already_rejected:
        return 0, set()

    # Cartesian (merchant variant) × (scraped variant) cosine via one SQL.
    # Filters live inside SQL so we never pull useless pairs out of Postgres.
    # Currency mismatch is tracked but never filters — pricing layer skips
    # mismatched rows; the match itself is still valuable for catalog mapping.
    rows = session.execute(
        text(
            'SELECT sv.id            AS merchant_variant_id, '
            '       pe."variantId"   AS competitor_variant_id, '
            '       pe."prodId"      AS competitor_prod_id, '
            '       sp_m.vendor      AS m_vendor, '
            '       sp_m."productType" AS m_type, '
            '       sp_m."productGender" AS m_gender, '
            '       sv."currentPrice" AS m_price, '
            '       sp_c.vendor      AS c_vendor, '
            '       sp_c."productType" AS c_type, '
            '       sp_c."productGender" AS c_gender, '
            '       scv."currentPrice" AS c_price, '
            '       ss.currency      AS shop_currency, '
            '       scv.currency     AS comp_currency, '
            '       se."vectorText" <=> pe."vectorText" AS dist_text, '
            '       CASE WHEN se."vectorImg" IS NOT NULL AND pe."vectorImg" IS NOT NULL '
            '            THEN se."vectorImg" <=> pe."vectorImg" '
            '            ELSE NULL END AS dist_img '
            'FROM "ShopifyVariant" sv '
            'JOIN "ShopifyProduct" sp_m ON sp_m.id = sv."productId" '
            'JOIN "ShopifyEmbedding" se ON se."variantId" = sv.id '
            'LEFT JOIN "ShopSettings" ss ON ss."shopDomain" = sp_m."shopDomain" '
            'CROSS JOIN "ProductEmbedding" pe '
            'JOIN "ScrapedProduct"  sp_c ON sp_c.id = pe."prodId" '
            'JOIN "ScrapedVariant"  scv ON scv.id = pe."variantId" '
            'WHERE sv."productId" = :mpid '
            '  AND pe."prodId"    = :cpid '
            '  AND pe."variantId" IS NOT NULL '
            '  AND se."vectorText" IS NOT NULL '
            '  AND pe."vectorText" IS NOT NULL '
            # Gender hard gate.
            '  AND ( sp_m."productGender" IS NULL OR sp_c."productGender" IS NULL '
            '        OR sp_m."productGender" = sp_c."productGender" ) '
        ),
        {"mpid": merchant_product_id, "cpid": scraped_product_id},
    ).all()

    if not rows:
        return 0, set()

    # Group by merchant variant → keep highest-scoring scraped variant per
    # merchant variant. (We're already inside one merchant product × one
    # scraped product, so this is the equivalent of the old "top-1 per
    # competitor product" rule applied per merchant variant.)
    best_by_mvariant: dict[str, dict] = {}
    pair_confidences: list[tuple[float, bool]] = []

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

        if score < ABSOLUTE_HYBRID_FLOOR:
            continue

        # currencyMismatch is only true when both sides have a known currency
        # and they differ. Null on either side stays permissive (False).
        currency_mismatch = bool(
            r.shop_currency and r.comp_currency and r.shop_currency != r.comp_currency
        )
        cand = {
            "merchant_variant_id":  r.merchant_variant_id,
            "competitor_variant_id": r.competitor_variant_id,
            "competitor_prod_id":   r.competitor_prod_id,
            "m_vendor":  r.m_vendor,
            "m_type":    r.m_type,
            "m_price":   float(r.m_price) if r.m_price is not None else None,
            "c_vendor":  r.c_vendor,
            "c_type":    r.c_type,
            "c_price":   float(r.c_price) if r.c_price is not None else None,
            "currency_mismatch": currency_mismatch,
            "dist_text": float(r.dist_text),
            "score":     score,
            "mtype":     mtype,
            "text_sim":  text_sim,
            "img_sim":   img_sim,
        }
        prev = best_by_mvariant.get(cand["merchant_variant_id"])
        if prev is None or cand["score"] > prev["score"]:
            best_by_mvariant[cand["merchant_variant_id"]] = cand

    if not best_by_mvariant:
        return 0, set()

    for cand in best_by_mvariant.values():
        confidence = compute_confidence(
            hybrid_sim=cand["score"],
            merchant_vendor=cand["m_vendor"],
            competitor_vendor=cand["c_vendor"],
            merchant_type=cand["m_type"],
            competitor_type=cand["c_type"],
            merchant_price=cand["m_price"],
            competitor_price=cand["c_price"],
        )
        if confidence <= 0:
            continue
        has_brand = brand_match(cand["m_vendor"], cand["c_vendor"])
        tier = confidence_tier(confidence, has_brand_match=has_brand)
        session.execute(
            text(
                'INSERT INTO "ProductMatch" '
                '(id, "shopDomain", "shopifyVariantId", "competitorVariantId", '
                ' "competitorProdId", "matchScore", "textScore", "imageScore", '
                ' "matchType", "vectorDistance", "thresholdUsed", '
                ' "confidence", "confidenceTier", "currencyMismatch", '
                ' "matchedAt", "updatedAt") '
                'VALUES (:id, :sd, :svid, :cvid, :cpid, :score, :tscore, :iscore, '
                '        :mtype, :dist, :thr, :conf, CAST(:tier AS "MatchConfidenceTier"), :cmis, NOW(), NOW()) '
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
                '  "currencyMismatch" = EXCLUDED."currencyMismatch", '
                '  "updatedAt"        = NOW()'
            ),
            {
                "id":     str(uuid.uuid4()),
                "sd":     shop_domain,
                "svid":   cand["merchant_variant_id"],
                "cvid":   cand["competitor_variant_id"],
                "cpid":   cand["competitor_prod_id"],
                "score":  round(cand["score"] * 100.0, 2),
                "tscore": round(cand["text_sim"] * 100.0, 2),
                "iscore": round(cand["img_sim"] * 100.0, 2) if cand["img_sim"] is not None else None,
                "mtype":  cand["mtype"],
                "dist":   round(cand["dist_text"], 6),
                # thresholdUsed is now constant (ABSOLUTE_HYBRID_FLOOR) — the
                # adaptive per-domain threshold is gone with the scoped flow.
                "thr":    round(ABSOLUTE_HYBRID_FLOOR, 4),
                "conf":   round(confidence, 3),
                "tier":   tier,
                "cmis":   cand["currency_mismatch"],
            },
        )
        written += 1
        touched_variants.add(cand["merchant_variant_id"])
        pair_confidences.append((confidence, has_brand))

    if not pair_confidences:
        return 0, set()

    # One ProductLevelMatch per (merchantProduct, scrapedProduct).
    # Confidence = max across variant pairs; brand evidence carried if ANY
    # variant pair carried it.
    pair_conf = max(c for c, _ in pair_confidences)
    pair_brand = any(b for _, b in pair_confidences)
    pair_tier = confidence_tier(pair_conf, has_brand_match=pair_brand)
    plm_row = session.execute(
        text(
            'INSERT INTO "ProductLevelMatch" '
            '(id, "shopDomain", "shopifyProductId", "scrapedProductId", '
            ' "confidence", "confidenceTier", source, "reviewStatus", '
            ' "createdAt", "updatedAt") '
            'VALUES (:id, :sd, :mpid, :cpid, :conf, CAST(:tier AS "MatchConfidenceTier"), '
            '        :src, \'PENDING\', NOW(), NOW()) '
            'ON CONFLICT ("shopifyProductId", "scrapedProductId") DO UPDATE SET '
            '  confidence       = GREATEST("ProductLevelMatch".confidence, EXCLUDED.confidence), '
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
            "mpid": merchant_product_id,
            "cpid": scraped_product_id,
            "conf": round(pair_conf, 3),
            "tier": pair_tier,
            "brand_match": pair_brand,
            "src":  "DISCOVERY",
            "confirmed": CONFIRMED_THRESHOLD,
            "likely":    LIKELY_THRESHOLD,
        },
    ).first()
    if plm_row:
        plm_id = plm_row[0]
        # Link every variant ProductMatch row in this pair to the product-level
        # match so downstream (pricing, confirmation UI) can navigate either way.
        session.execute(
            text(
                'UPDATE "ProductMatch" '
                'SET "productMatchId" = :plm '
                'WHERE "shopifyVariantId" = ANY(:svids) '
                '  AND "competitorProdId" = :cpid'
            ),
            {
                "plm":   plm_id,
                "svids": list(touched_variants),
                "cpid":  scraped_product_id,
            },
        )

    return written, touched_variants


# ─────────────────────────────────────────────────────────────────────────────
# Celery task
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name="matcher.match_for_scraped_product", bind=True, max_retries=3, default_retry_delay=60)
def match_for_scraped_product(self, scraped_product_id: str):
    """Match this scraped product against every merchant product that
    triggered its discovery (via CompetitorCandidate)."""
    total_written = 0
    touched_variants: set[str] = set()
    shop_domain: str | None = None

    try:
        with get_db() as session:
            shop_row = session.execute(
                text('SELECT "shopDomain" FROM "ScrapedProduct" WHERE id = :pid'),
                {"pid": scraped_product_id},
            ).first()
            if not shop_row:
                logger.warning("scraped_product_not_found", scraped_product_id=scraped_product_id)
                return 0
            shop_domain = shop_row[0]

            # All merchant products that asked for this scrape (non-rejected
            # candidates). REJECTED candidates were thrown out at discovery
            # review and shouldn't produce matches.
            candidate_rows = session.execute(
                text(
                    'SELECT DISTINCT "shopifyProductId" '
                    'FROM "CompetitorCandidate" '
                    'WHERE "scrapedProductId" = :pid '
                    '  AND "status" != CAST(\'REJECTED\' AS "CandidateStatus")'
                ),
                {"pid": scraped_product_id},
            ).all()
            merchant_product_ids = [r[0] for r in candidate_rows if r[0]]

            if not merchant_product_ids:
                logger.info("no_eligible_merchant_candidates", scraped_product_id=scraped_product_id)
                return 0

            for mpid in merchant_product_ids:
                written, touched = _match_one_pair(
                    session,
                    shop_domain=shop_domain,
                    merchant_product_id=mpid,
                    scraped_product_id=scraped_product_id,
                )
                total_written += written
                touched_variants.update(touched)

            # Mark this scrape's competitor embeddings as consumed by the matcher.
            session.execute(
                text(
                    'UPDATE "ProductEmbedding" SET "matchedAt" = NOW() '
                    'WHERE "prodId" = :pid AND "vectorText" IS NOT NULL'
                ),
                {"pid": scraped_product_id},
            )
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.exception("match_permanently_failed", scraped_product_id=scraped_product_id)
            return 0
        logger.exception("match_failed_retrying", scraped_product_id=scraped_product_id)
        raise self.retry(exc=exc)

    logger.info(
        "match_run_completed",
        scraped_product_id=scraped_product_id,
        matches_written=total_written,
        variants_touched=len(touched_variants),
    )

    if total_written == 0:
        return 0

    # Stats recomputation per merchant variant whose match set changed.
    assert shop_domain is not None
    for svid in touched_variants:
        try:
            app.send_task(
                "stats.recompute_for_variant",
                args=[shop_domain, svid],
                queue="stats_queue",
            )
        except Exception:
            logger.exception(
                "recompute_for_variant_dispatch_failed",
                shop_domain=shop_domain,
                shopify_variant_id=svid,
            )

    return total_written
