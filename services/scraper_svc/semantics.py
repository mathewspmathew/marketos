"""
services/scraper_svc/semantics.py

Task 3 — generate_variant_semantics (semantic_queue)
  One Groq call for all ScrapedVariants → bulk-update semanticText → queue embeddings.

Task 4 — generate_shopify_variant_semantics (semantic_queue)
  Same flow for ShopifyVariants. Triggered by the API gateway on product create/update.
"""

import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from groq import Groq, RateLimitError as GroqRateLimitError
from sqlalchemy import update as sa_update

from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ScrapedProduct, ScrapedVariant, ShopifyProduct, ShopifyVariant
from services.scraper_svc.helpers import log_error

load_dotenv()

_groq_client = Groq(api_key=os.getenv("GROQ_API_KEY", "not-set"))

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
# Groq helpers
# ─────────────────────────────────────────────────────────────────────────────

def _groq_semantic_call(prompt: str) -> dict[str, str]:
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


def _build_semantic_prompt(
    title: str,
    vendor: str | None,
    product_type: str | None,
    description: str | None,
    tags: list | str,
    specs: dict | None,
    variants_payload: list[dict],
) -> str:
    return GROQ_SEMANTIC_PROMPT.format(
        title=title,
        vendor=vendor or "Unknown Brand",
        product_type=product_type or "Product",
        description=(description or "")[:500],
        tags=", ".join(tags) if isinstance(tags, list) else str(tags),
        specs=json.dumps(specs or {}, ensure_ascii=False),
        variants_json=json.dumps(variants_payload, ensure_ascii=False),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Task 3: generate_variant_semantics
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name='scraper.generate_variant_semantics', bind=True, max_retries=3, default_retry_delay=30, rate_limit='3/m')
def generate_variant_semantics(self, product_id: str, config_id: str, shop_domain: str, product_url: str):
    """One Groq call generates semanticText for all ScrapedVariants, then queues embeddings."""
    print(f"[>] Generating semantic text for product {product_id}")

    try:
        with get_db() as session:
            product  = session.query(ScrapedProduct).filter(ScrapedProduct.id == product_id).first()
            variants = session.query(ScrapedVariant).filter(ScrapedVariant.productId == product_id).all()

            if not product:
                print(f"[!] Product {product_id} not found — skipping")
                return
            if not variants:
                print(f"[!] No variants for product {product_id} — skipping")
                return

            variants_payload = [
                {
                    "id":             v.id,
                    "title":          v.title,
                    "options":        v.options or {},
                    "current_price":  float(v.currentPrice or 0),
                    "original_price": float(v.originalPrice) if v.originalPrice else None,
                    "is_in_stock":    v.isInStock,
                }
                for v in variants
            ]

            try:
                semantic_map = _groq_semantic_call(
                    _build_semantic_prompt(
                        product.title, product.vendor, product.productType,
                        product.description, product.tags, product.specifications,
                        variants_payload,
                    )
                )
            except GroqRateLimitError:
                raise self.retry(countdown=65)
            except Exception as e:
                if self.request.retries >= self.max_retries:
                    log_error(shop_domain, config_id, product_url, "SEMANTIC_FAILED", 'scraper.generate_variant_semantics', detail=str(e))
                    return
                raise self.retry(exc=e)

            now     = datetime.now(timezone.utc)
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

    except Exception as exc:
        raise self.retry(exc=exc)

    app.send_task('embedder.generate_embeddings', args=[product_id], queue='embedding_queue')
    print(f"    [>] Queued embedding: {product_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Task 4: generate_shopify_variant_semantics
# ─────────────────────────────────────────────────────────────────────────────

def _short_id(gid: str) -> str:
    """Extract the numeric tail from a Shopify GID so the LLM gets a simple key."""
    return gid.rsplit("/", 1)[-1]


@app.task(name='scraper.generate_shopify_variant_semantics', bind=True, max_retries=3, default_retry_delay=30, rate_limit='3/m')
def generate_shopify_variant_semantics(self, product_id: str):
    """One Groq call generates semanticText for all ShopifyVariants, then queues embeddings."""
    print(f"[>] Generating Shopify semantic text for product {product_id}")

    updated_ids = []

    try:
        with get_db() as session:
            product  = session.query(ShopifyProduct).filter(ShopifyProduct.id == product_id).first()
            variants = session.query(ShopifyVariant).filter(
                ShopifyVariant.productId == product_id,
                ShopifyVariant.semanticText == None,  # noqa: E711
            ).all()

            if not product:
                print(f"[!] ShopifyProduct {product_id} not found — skipping")
                return
            if not variants:
                print(f"[!] No variants needing semanticText for ShopifyProduct {product_id} — skipping")
                return

            # Use the numeric tail of the GID as the Groq key — LLMs reproduce
            # short integers reliably, unlike full GID strings with slashes/colons.
            id_map = {_short_id(v.id): v.id for v in variants}

            variants_payload = [
                {
                    "id":             _short_id(v.id),
                    "title":          v.title,
                    "options":        v.options or {},
                    "current_price":  float(v.currentPrice or 0),
                    "original_price": float(v.compareAtPrice) if v.compareAtPrice else None,
                    "is_in_stock":    v.isInStock,
                }
                for v in variants
            ]

            try:
                semantic_map = _groq_semantic_call(
                    _build_semantic_prompt(
                        product.title, product.vendor, product.productType,
                        product.description, product.tags, None,
                        variants_payload,
                    )
                )
            except GroqRateLimitError:
                raise self.retry(countdown=65)
            except Exception as e:
                if self.request.retries >= self.max_retries:
                    print(f"[!] Giving up on Shopify semantics for {product_id}: {e}")
                    return
                raise self.retry(exc=e)

            now = datetime.now(timezone.utc)
            for short_key, full_id in id_map.items():
                text = semantic_map.get(short_key, "")
                if text:
                    session.execute(
                        sa_update(ShopifyVariant)
                        .where(ShopifyVariant.id == full_id)
                        .values(semanticText=text, updatedAt=now)
                    )
                    updated_ids.append(full_id)

            print(f"    [✓] semanticText written for {len(updated_ids)}/{len(variants)} Shopify variant(s) of '{product.title[:40]}'")

    except Exception as exc:
        raise self.retry(exc=exc)

    for variant_id in updated_ids:
        app.send_task('shopify_embedder.generate_shopify_embeddings', args=[variant_id], queue='embedding_queue')
        print(f"    [>] Queued Shopify embedding: {variant_id[:8]}")
