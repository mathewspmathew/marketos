"""
services/suggestion_svc/main.py

Tasks (suggestion_queue):
  suggestion.suggest_for_shop(shop_domain, scope='all'|'first_time_and_showed')
    Find every ShopifyProduct in this shop that has at least one variant with
    a ProductMatch row scoring >= MATCH_THRESHOLD, and fan out per-product
    suggestion tasks. Skips products whose ProductSuggestion is APPLIED unless
    scope='all'.

  suggestion.suggest_for_product(shop_domain, shopify_product_id)
    For one merchant product:
      1. Pull its variants.
      2. For each variant, gather competitor ScrapedVariants linked via
         ProductMatch with matchScore >= MATCH_THRESHOLD.
      3. Filter prices to INR-only / non-outlier (IQR fence).
      4. Compute competitor min/median/max per variant -> upsert
         VariantPriceSuggestion.
      5. Aggregate competitor titles+descriptions across all qualifying
         variants -> single Groq call -> upsert ProductSuggestion (title +
         descriptionHtml + rationale).
    Edited fields (editedTitle / editedDescriptionHtml / chosenPrice) are
    preserved across regenerations: we only overwrite suggested* values.

Read paths: ShopifyProduct, ShopifyVariant, ProductMatch, ScrapedVariant.
Write paths: ProductSuggestion, VariantPriceSuggestion (UPSERT).

Shopify write-back is NOT done here — the UI route applies user-approved
values via the merchant's session token.
"""
from __future__ import annotations

import json
import os
import statistics
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from dotenv import load_dotenv
from groq import Groq, RateLimitError as GroqRateLimitError
from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
MATCH_THRESHOLD = Decimal("65.00")     # ProductMatch.matchScore cutoff
LLM_MAX_COMPETITORS = 30               # cap competitor inputs into Groq prompt
INR_MIN, INR_MAX = Decimal("1"), Decimal("10000000")  # sanity bounds, INR

_groq_client = Groq(api_key=os.getenv("GROQ_API_KEY", "not-set"))


# ─────────────────────────────────────────────────────────────────────────────
# Pricing helpers
# ─────────────────────────────────────────────────────────────────────────────
def _filter_inr_prices(prices: list[Decimal]) -> list[Decimal]:
    """Drop sentinels and outliers. Currency check is best-effort: we accept
    anything inside a plausible INR range. Refine once ScrapedVariant carries
    explicit currency."""
    cleaned = [p for p in prices if p is not None and INR_MIN <= p <= INR_MAX]
    if len(cleaned) < 4:
        return cleaned
    cleaned_sorted = sorted(cleaned)
    n = len(cleaned_sorted)
    q1 = cleaned_sorted[n // 4]
    q3 = cleaned_sorted[(3 * n) // 4]
    iqr = q3 - q1
    lo, hi = q1 - Decimal("1.5") * iqr, q3 + Decimal("1.5") * iqr
    return [p for p in cleaned_sorted if lo <= p <= hi]


def _price_aggregates(prices: list[Decimal]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if not prices:
        return None, None, None
    ordered = sorted(prices)
    median = Decimal(str(statistics.median([float(p) for p in ordered]))).quantize(Decimal("0.01"))
    return ordered[0], median, ordered[-1]


# ─────────────────────────────────────────────────────────────────────────────
# Groq content prompt
# ─────────────────────────────────────────────────────────────────────────────
GROQ_CONTENT_PROMPT = """You are improving a Shopify product listing using competitor data.

Current product:
  title: {current_title}
  description: {current_description}
  vendor: {vendor}

Top competitor products (matched by semantic similarity, INR market):
{competitors_json}

Return a JSON object with exactly these keys:
  "title":           a concise, SEO-friendly product title (<= 80 chars)
  "description_html": an HTML product description (<p>, <ul>, <li>, <strong> only). Highlight differentiators visible in competitor data; preserve brand voice; no fabricated specs.
  "rationale":       1-2 sentences explaining what changed and why.

JSON only. No markdown, no extra keys."""


def _groq_content_call(prompt: str) -> dict:
    response = _groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": "Output JSON only."},
            {"role": "user",   "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    return json.loads(response.choices[0].message.content)


# ─────────────────────────────────────────────────────────────────────────────
# Fan-out: suggest_for_shop
# ─────────────────────────────────────────────────────────────────────────────
@app.task(name='suggestion.suggest_for_shop')
def suggest_for_shop(shop_domain: str, scope: str = "first_time_and_showed") -> int:
    """Enqueue per-product suggestion tasks for every product with >=1 qualifying match."""
    with get_db() as session:
        rows = session.execute(
            text("""
                SELECT DISTINCT sv."productId" AS product_id
                FROM "ProductMatch" pm
                JOIN "ShopifyVariant" sv ON sv."id" = pm."shopifyVariantId"
                LEFT JOIN "ProductSuggestion" ps ON ps."shopifyProductId" = sv."productId"
                WHERE pm."shopDomain" = :shop
                  AND pm."matchScore" >= :threshold
                  AND pm."dismissedAt" IS NULL
                  AND (
                    :scope = 'all'
                    OR ps."status" IS NULL
                    OR ps."status" IN ('FIRST_TIME', 'SHOWED')
                  )
            """),
            {"shop": shop_domain, "threshold": MATCH_THRESHOLD, "scope": scope},
        ).fetchall()

    queued = 0
    for r in rows:
        suggest_for_product.delay(shop_domain, r.product_id)
        queued += 1
    print(f"[suggestion] queued {queued} products for {shop_domain} (scope={scope})", flush=True)
    return queued


# ─────────────────────────────────────────────────────────────────────────────
# Per-product worker
# ─────────────────────────────────────────────────────────────────────────────
@app.task(name='suggestion.suggest_for_product', bind=True, max_retries=3, default_retry_delay=30, rate_limit='3/m')
def suggest_for_product(self, shop_domain: str, shopify_product_id: str) -> dict:
    now = datetime.now(timezone.utc)
    written = {"variants": 0, "product": False}

    with get_db() as session:
        product = session.execute(
            text("""
                SELECT id, title, description, vendor
                FROM "ShopifyProduct"
                WHERE id = :pid AND "shopDomain" = :shop
            """),
            {"pid": shopify_product_id, "shop": shop_domain},
        ).first()
        if not product:
            print(f"[suggestion] product {shopify_product_id} not found for {shop_domain}", flush=True)
            return written

        variants = session.execute(
            text("""SELECT id, "currentPrice", title FROM "ShopifyVariant" WHERE "productId" = :pid"""),
            {"pid": shopify_product_id},
        ).fetchall()

        # ── 1. Per-variant price aggregates ──────────────────────────────
        all_competitor_ctx: list[dict] = []
        all_match_scores: list[Decimal] = []

        for v in variants:
            comp_rows = session.execute(
                text("""
                    SELECT sv."currentPrice" AS price,
                           sv."title"        AS title,
                           sp."description"  AS description,
                           sp."vendor"       AS vendor,
                           pm."matchScore"   AS score
                    FROM "ProductMatch" pm
                    JOIN "ScrapedVariant" sv ON sv."id" = pm."competitorVariantId"
                    JOIN "ScrapedProduct" sp ON sp."id" = sv."productId"
                    WHERE pm."shopifyVariantId" = :vid
                      AND pm."matchScore" >= :threshold
                      AND pm."dismissedAt" IS NULL
                    ORDER BY pm."matchScore" DESC
                """),
                {"vid": v.id, "threshold": MATCH_THRESHOLD},
            ).fetchall()

            if not comp_rows:
                continue

            prices_filtered = _filter_inr_prices([r.price for r in comp_rows])
            cmin, cmed, cmax = _price_aggregates(prices_filtered)

            rationale = (
                f"From {len(prices_filtered)} INR competitors (of {len(comp_rows)} matches >= {MATCH_THRESHOLD})."
                if prices_filtered else
                "No usable INR competitor prices."
            )

            session.execute(
                text("""
                    INSERT INTO "VariantPriceSuggestion"
                      (id, "shopDomain", "shopifyVariantId",
                       "competitorMin", "competitorMedian", "competitorMax",
                       "competitorCount", "priceRationale",
                       "status", "generatedAt", "updatedAt")
                    VALUES
                      (:id, :shop, :vid, :cmin, :cmed, :cmax, :ccount, :rationale,
                       'FIRST_TIME'::"SuggestionStatus", :now, :now)
                    ON CONFLICT ("shopifyVariantId") DO UPDATE SET
                      "competitorMin"    = EXCLUDED."competitorMin",
                      "competitorMedian" = EXCLUDED."competitorMedian",
                      "competitorMax"    = EXCLUDED."competitorMax",
                      "competitorCount"  = EXCLUDED."competitorCount",
                      "priceRationale"   = EXCLUDED."priceRationale",
                      "generatedAt"      = EXCLUDED."generatedAt",
                      "updatedAt"        = EXCLUDED."updatedAt",
                      "status"           = CASE
                        WHEN "VariantPriceSuggestion"."status" = 'APPLIED'::"SuggestionStatus"
                          THEN 'APPLIED'::"SuggestionStatus"
                        ELSE 'SHOWED'::"SuggestionStatus"
                      END
                """),
                {
                    "id": str(uuid.uuid4()),
                    "shop": shop_domain,
                    "vid": v.id,
                    "cmin": cmin, "cmed": cmed, "cmax": cmax,
                    "ccount": len(prices_filtered),
                    "rationale": rationale,
                    "now": now,
                },
            )
            written["variants"] += 1

            # collect for product-level LLM context
            for r in comp_rows[:LLM_MAX_COMPETITORS]:
                all_competitor_ctx.append({
                    "title": r.title,
                    "description": (r.description or "")[:500],
                    "vendor": r.vendor,
                    "price_inr": float(r.price) if r.price is not None else None,
                    "match_score": float(r.score),
                })
                all_match_scores.append(r.score)

        # ── 2. Product-level LLM content ────────────────────────────────
        if not all_competitor_ctx:
            print(f"[suggestion] no qualifying competitors for product {shopify_product_id}", flush=True)
            return written

        # dedupe + cap
        seen = set()
        deduped: list[dict] = []
        for c in sorted(all_competitor_ctx, key=lambda x: -x["match_score"]):
            key = (c["title"], c["vendor"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)
            if len(deduped) >= LLM_MAX_COMPETITORS:
                break

        prompt = GROQ_CONTENT_PROMPT.format(
            current_title=product.title,
            current_description=(product.description or "")[:800],
            vendor=product.vendor or "Unknown Brand",
            competitors_json=json.dumps(deduped, ensure_ascii=False),
        )

        try:
            llm = _groq_content_call(prompt)
        except GroqRateLimitError:
            raise self.retry(countdown=65)
        except Exception as e:
            if self.request.retries >= self.max_retries:
                print(f"[suggestion] groq failed for {shopify_product_id}: {e}", flush=True)
                return written
            raise self.retry(exc=e)

        suggested_title = (llm.get("title") or "").strip() or None
        suggested_desc  = (llm.get("description_html") or "").strip() or None
        rationale       = (llm.get("rationale") or "").strip() or None

        avg_score = (
            sum(all_match_scores) / Decimal(len(all_match_scores))
        ).quantize(Decimal("0.01")) if all_match_scores else None

        session.execute(
            text("""
                INSERT INTO "ProductSuggestion"
                  (id, "shopDomain", "shopifyProductId",
                   "suggestedTitle", "suggestedDescriptionHtml", "contentRationale",
                   "matchCount", "avgMatchScore",
                   "status", "generatedAt", "updatedAt")
                VALUES
                  (:id, :shop, :pid, :title, :desc, :rationale,
                   :mcount, :avg, 'FIRST_TIME'::"SuggestionStatus", :now, :now)
                ON CONFLICT ("shopifyProductId") DO UPDATE SET
                  "suggestedTitle"           = EXCLUDED."suggestedTitle",
                  "suggestedDescriptionHtml" = EXCLUDED."suggestedDescriptionHtml",
                  "contentRationale"         = EXCLUDED."contentRationale",
                  "matchCount"               = EXCLUDED."matchCount",
                  "avgMatchScore"            = EXCLUDED."avgMatchScore",
                  "generatedAt"              = EXCLUDED."generatedAt",
                  "updatedAt"                = EXCLUDED."updatedAt",
                  "status"                   = CASE
                    WHEN "ProductSuggestion"."status" = 'APPLIED'::"SuggestionStatus"
                      THEN 'APPLIED'::"SuggestionStatus"
                    ELSE 'SHOWED'::"SuggestionStatus"
                  END
            """),
            {
                "id": str(uuid.uuid4()),
                "shop": shop_domain,
                "pid": shopify_product_id,
                "title": suggested_title,
                "desc": suggested_desc,
                "rationale": rationale,
                "mcount": len(deduped),
                "avg": avg_score,
                "now": now,
            },
        )
        written["product"] = True

    print(f"[suggestion] {shopify_product_id}: variants={written['variants']} product={written['product']}", flush=True)
    return written
