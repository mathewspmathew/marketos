"""
services/suggestion_svc/main.py

Content-only suggestion service. Pricing was moved to pricing_svc; this file
no longer writes VariantPriceSuggestion. The pricing path (rules + stats +
shopify_writer) is the single source of truth for prices.

Tasks (suggestion_queue):
  suggestion.suggest_for_shop(shop_domain, scope='all'|'first_time_and_showed')
    Find every ShopifyProduct with at least one qualifying ProductMatch and
    fan out per-product suggestion tasks.

  suggestion.suggest_for_product(shop_domain, shopify_product_id)
    For one merchant product:
      1. Pull competitor titles + descriptions across all qualifying variants.
      2. Single Groq call -> upsert ProductSuggestion (title +
         descriptionHtml + rationale).

Read paths: ShopifyProduct, ShopifyVariant, ProductMatch, ScrapedVariant.
Write paths: ProductSuggestion (UPSERT).

Shopify write-back is NOT done here — the UI route applies user-approved
title/description via the merchant's session token.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from dotenv import load_dotenv
from groq import RateLimitError as GroqRateLimitError

from services.common.groq_client import make_groq_client
from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
MATCH_THRESHOLD = Decimal("65.00")     # ProductMatch.matchScore cutoff
LLM_MAX_COMPETITORS = 30               # cap competitor inputs into Groq prompt

_groq_client = make_groq_client()


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
# Per-product worker — content only (title + description)
# ─────────────────────────────────────────────────────────────────────────────
@app.task(name='suggestion.suggest_for_product', bind=True, max_retries=3, default_retry_delay=30, rate_limit='3/m')
def suggest_for_product(self, shop_domain: str, shopify_product_id: str) -> dict:
    now = datetime.now(timezone.utc)
    written = {"product": False}

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

        # Pull qualifying competitor context across every variant of the
        # product in one shot — we only need the LLM context, not per-variant
        # price aggregates anymore.
        comp_rows = session.execute(
            text("""
                SELECT sv2."currentPrice" AS price,
                       sv2."title"        AS title,
                       sp."description"   AS description,
                       sp."vendor"        AS vendor,
                       pm."matchScore"    AS score
                FROM "ProductMatch" pm
                JOIN "ShopifyVariant" sv  ON sv.id  = pm."shopifyVariantId"
                JOIN "ScrapedVariant" sv2 ON sv2.id = pm."competitorVariantId"
                JOIN "ScrapedProduct" sp  ON sp.id  = sv2."productId"
                WHERE sv."productId"   = :pid
                  AND pm."shopDomain"  = :shop
                  AND pm."matchScore"  >= :threshold
                  AND pm."dismissedAt" IS NULL
                ORDER BY pm."matchScore" DESC
                LIMIT :cap
            """),
            {"pid": shopify_product_id, "shop": shop_domain,
             "threshold": MATCH_THRESHOLD, "cap": LLM_MAX_COMPETITORS * 3},
        ).fetchall()

        if not comp_rows:
            print(f"[suggestion] no qualifying competitors for product {shopify_product_id}", flush=True)
            return written

        # dedupe by (title, vendor) and cap to LLM_MAX_COMPETITORS
        seen = set()
        deduped: list[dict] = []
        all_match_scores: list[Decimal] = []
        for r in comp_rows:
            key = (r.title, r.vendor)
            if key in seen:
                continue
            seen.add(key)
            deduped.append({
                "title":       r.title,
                "description": (r.description or "")[:500],
                "vendor":      r.vendor,
                "price_inr":   float(r.price) if r.price is not None else None,
                "match_score": float(r.score),
            })
            all_match_scores.append(r.score)
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

    print(f"[suggestion] {shopify_product_id}: product={written['product']}", flush=True)
    return written
