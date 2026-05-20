"""
services/scraper_svc/semantics.py

Task 3 — generate_variant_semantics (semantic_queue)
  One Groq call for all ScrapedVariants → bulk-update semanticText → queue embeddings.
  Uses GROQ_API_KEY.

Task 4 — generate_shopify_variant_semantics (shopify_semantic_queue)
  Same flow for ShopifyVariants. Triggered by the API gateway on product create/update.
  Uses GROQ_API_KEY_SHOPIFY (separate key for TPM isolation from competitor scraping).
"""

import json
import os
from datetime import datetime, timezone

from celery.exceptions import Retry as CeleryRetry
from dotenv import load_dotenv
from groq import Groq, RateLimitError as GroqRateLimitError
from sqlalchemy import text as sa_text, update as sa_update

from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ScrapedProduct, ScrapedVariant, ShopifyProduct, ShopifyVariant
from services.common.vertex_embed import (
    embed_text,
    invalidate_search_query_vector,
    save_search_query_vector,
)
from services.scraper_svc.helpers import log_error

load_dotenv()

# Two separate Groq clients so competitor (scraper) and Shopify-side semantic
# generation each get their own TPM budget on the Groq free tier.
_groq_client         = Groq(api_key=os.getenv("GROQ_API_KEY", "not-set"))
_groq_client_shopify = Groq(api_key=os.getenv("GROQ_API_KEY_SHOPIFY", os.getenv("GROQ_API_KEY", "not-set")))

GROQ_SEMANTIC_PROMPT = """You are a product cataloguing assistant. For each variant below
produce a STRUCTURED FINGERPRINT used to compare this product against the same product
listed on other stores. Two listings of the same physical product MUST produce nearly
identical text. Different products MUST produce clearly different text.

Hard rules:
- NO marketing language, NO synonyms, NO buyer prose, NO use-case suggestions.
- NO price, NO discount, NO availability, NO stock state.
- Lower-case everywhere except proper nouns (brand names, model names, ISBNs, etc.).
- Stable, deterministic, attribute-anchored. Same product on two sites → same text.
- Works for ANY category: clothing, electronics, books, kitchenware, bags, beauty,
  furniture, food, toys, tools, jewellery — pick the attributes that matter for THIS
  product's category. Leave fields blank if unknown; never invent values.

Output format per variant (literal six lines, in this order, with these labels):
BRAND: <brand name or 'unknown'>
CATEGORY: <top-level> > <subcategory>          (e.g. clothing > t-shirt,
                                                 electronics > laptop,
                                                 kitchenware > frying-pan,
                                                 books > fiction-novel,
                                                 bags > backpack)
MODEL: <product line / model number / book title / sku family — empty if none>
ATTRIBUTES: key=value; key=value; key=value; ...
            (pick 4–8 defining attributes for this category. Examples:
             clothing: material, color, fit, sleeve, neckline, pattern, gender, size
             electronics: cpu, ram, storage, screen, ports, battery, color
             books: author, genre, format, language, pages, isbn
             kitchenware: material, diameter_or_capacity, induction, coating
             bags: capacity_l, material, color, laptop_fit_in, waterproof
             beauty: shade, finish, volume, vegan, spf
             furniture: material, dimensions, color, assembly_required
             — use lower_snake_case keys. Values lower-case unless proper noun.)
IDENTITY: <one short factual phrase, max 12 words, uniquely fingerprinting this product
           — typically brand + model + key disambiguating attribute. No prose, no fluff.>
GENDER: <men / women / unisex / kids / not-applicable>

Product context:
  Name: {title}
  Brand: {vendor}
  Category hint: {product_type}
  Description (truncated, may contain marketing fluff — extract facts only): {description}
  Tags: {tags}
  Specifications: {specs}

Variants to describe (use the exact IDs as JSON keys):
{variants_json}

Return ONLY valid JSON: {{"<variant_id>": "<six-line structured fingerprint>", ...}}
One key per variant ID provided. The value is the literal six-line block — embed newlines
as \\n in the JSON string. No markdown, no extra keys, no commentary."""


# ─────────────────────────────────────────────────────────────────────────────
# Groq helpers
# ─────────────────────────────────────────────────────────────────────────────

def _groq_semantic_call(prompt: str, *, shopify: bool = False) -> dict[str, str]:
    client = _groq_client_shopify if shopify else _groq_client
    response = client.chat.completions.create(
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
# searchQuery generation — a compact 5-7 keyword phrase used to retrieve this
# product on Google. Separate Groq call from the semantic fingerprint so the
# two have independent prompts and one can fail without taking the other down.
# ─────────────────────────────────────────────────────────────────────────────
GROQ_SEARCH_QUERY_PROMPT = """Produce a Google search query that will retrieve product-detail pages
selling the same product. Output 4–7 plain lowercase keywords, no quotes, no punctuation.

Rules:
- Lead with brand + product model/family if known.
- Include the most disambiguating attribute (color, size, capacity, edition, etc.).
- Do NOT include marketing words, adjectives like "best", "premium", or descriptors of intent
  like "online" or "buy".
- Do NOT include the merchant's own store domain or city/country names.
- If the product is generic (no brand/model), use category + 1–2 strong attributes.

Product:
  title: {title}
  vendor: {vendor}
  category: {category}
  description: {description}

Return STRICT JSON: {{"query": "your keywords here"}}"""


def _groq_search_query(
    *,
    title: str,
    vendor: str | None,
    category: str | None,
    description: str | None,
    shopify: bool = True,
) -> str | None:
    """One small Groq call → product-level search query string, or None on failure."""
    client = _groq_client_shopify if shopify else _groq_client
    prompt = GROQ_SEARCH_QUERY_PROMPT.format(
        title=title or "",
        vendor=vendor or "Unknown Brand",
        category=category or "Product",
        description=(description or "")[:400],
    )
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "Output JSON only."},
                {"role": "user",   "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        data = json.loads(response.choices[0].message.content)
        q = (data.get("query") or "").strip().lower()
        # Keep keywords-only: strip punctuation noise an LLM sometimes adds.
        q = " ".join(
            tok for tok in q.replace(",", " ").replace('"', " ").split() if tok
        )
        return q or None
    except Exception as exc:
        print(f"[!] search query generation failed: {exc}")
        return None


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


_VALID_GENDERS = {"men", "women", "unisex", "kids", "not-applicable"}


def _parse_fingerprint_fields(fingerprint: str) -> dict[str, str | None]:
    """Extract structured columns from a six-line semanticText fingerprint.

    Returns {'categoryTop': str|None, 'productGender': str|None}.
    Tolerates whitespace, missing lines, malformed values. Never raises.
    """
    category_top: str | None = None
    gender: str | None = None
    for raw_line in (fingerprint or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("category:"):
            value = line.split(":", 1)[1].strip().lower()
            # "clothing > t-shirt" → "clothing"; "kitchenware" → "kitchenware"
            top = value.split(">", 1)[0].strip()
            if top and top != "unknown":
                category_top = top
        elif line.lower().startswith("gender:"):
            value = line.split(":", 1)[1].strip().lower()
            if value in _VALID_GENDERS:
                gender = value
    return {"categoryTop": category_top, "productGender": gender}


def _consolidate_product_fields(parsed_per_variant: list[dict]) -> dict[str, str | None]:
    """Pick one categoryTop + one productGender for the product.

    All variants of the same product should yield the same category/gender, but
    the LLM occasionally varies. We take the most common non-null value per
    field; ties are broken by first occurrence.
    """
    from collections import Counter
    cat = Counter(p["categoryTop"] for p in parsed_per_variant if p["categoryTop"])
    gen = Counter(p["productGender"] for p in parsed_per_variant if p["productGender"])
    return {
        "categoryTop":   cat.most_common(1)[0][0] if cat else None,
        "productGender": gen.most_common(1)[0][0] if gen else None,
    }


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
            parsed_per_variant: list[dict] = []
            for v in variants:
                text = semantic_map.get(v.id, "")
                if text:
                    session.execute(
                        sa_update(ScrapedVariant)
                        .where(ScrapedVariant.id == v.id)
                        .values(semanticText=text, updatedAt=now)
                    )
                    updated += 1
                    parsed_per_variant.append(_parse_fingerprint_fields(text))

            if parsed_per_variant:
                product_fields = _consolidate_product_fields(parsed_per_variant)
                if product_fields["categoryTop"] or product_fields["productGender"]:
                    session.execute(
                        sa_update(ScrapedProduct)
                        .where(ScrapedProduct.id == product_id)
                        .values(
                            categoryTop=product_fields["categoryTop"],
                            productGender=product_fields["productGender"],
                            updatedAt=now,
                        )
                    )

            print(f"    [✓] semanticText written for {updated}/{len(variants)} variant(s) of '{product.title[:40]}'")

            if updated == 0:
                if self.request.retries >= self.max_retries:
                    log_error(shop_domain, config_id, product_url, "SEMANTIC_NO_MATCH",
                              'scraper.generate_variant_semantics',
                              detail="Groq returned no matching variant IDs after max retries")
                    return
                raise self.retry(exc=ValueError(f"Groq returned no matching variant IDs for {product_id}"))

    except CeleryRetry:
        raise
    except Exception as exc:
        raise self.retry(exc=exc)

    try:
        app.send_task('embedder.generate_embeddings', args=[product_id], queue='embedding_queue')
        print(f"    [>] Queued embedding: {product_id}")
    except Exception as e:
        print(f"    [!] Failed to queue embedding for {product_id}: {e} — will retry on next semantic run", flush=True)
        raise self.retry(exc=e)


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
            if not product:
                print(f"[!] ShopifyProduct {product_id} not found — skipping")
                return

            variants = session.query(ShopifyVariant).filter(
                ShopifyVariant.productId == product_id,
                ShopifyVariant.semanticText == None,  # noqa: E711
            ).all()

            needs_search_query = (
                not product.searchQuery
                and not product.searchQueryOverride
            )

            # If neither variants nor the product itself need anything, we're done.
            if not variants and not needs_search_query:
                print(f"[!] Nothing to generate for ShopifyProduct {product_id} — skipping")
                return

            now = datetime.now(timezone.utc)
            parsed_per_variant: list[dict] = []

            # ── Variant-level semanticText (only for variants missing it) ──
            if variants:
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
                        ),
                        shopify=True,
                    )
                except GroqRateLimitError:
                    raise self.retry(countdown=65)
                except Exception as e:
                    if self.request.retries >= self.max_retries:
                        print(f"[!] Giving up on Shopify semantics for {product_id}: {e}")
                        return
                    raise self.retry(exc=e)

                for short_key, full_id in id_map.items():
                    text = semantic_map.get(short_key, "")
                    if text:
                        session.execute(
                            sa_update(ShopifyVariant)
                            .where(ShopifyVariant.id == full_id)
                            .values(semanticText=text, updatedAt=now)
                        )
                        updated_ids.append(full_id)
                        parsed_per_variant.append(_parse_fingerprint_fields(text))

            product_fields = _consolidate_product_fields(parsed_per_variant) if parsed_per_variant else {}
            category_for_query = product_fields.get("categoryTop") if product_fields else (product.categoryTop or None)
            if product_fields and (product_fields.get("categoryTop") or product_fields.get("productGender")):
                session.execute(
                    sa_update(ShopifyProduct)
                    .where(ShopifyProduct.id == product_id)
                    .values(
                        categoryTop=product_fields["categoryTop"],
                        productGender=product_fields["productGender"],
                        updatedAt=now,
                    )
                )

            # ── searchQuery generation (runs independently of variants) ────
            # Generate when:
            #   - no override pinned, AND
            #   - product.searchQuery is currently empty
            # If an override IS pinned, we don't touch searchQuery but we DO
            # refresh the vector cache against the effective (override) query.
            if not product.searchQueryOverride and not product.searchQuery:
                new_query = _groq_search_query(
                    title=product.title,
                    vendor=product.vendor,
                    category=category_for_query or product.productType,
                    description=product.description,
                )
                if new_query:
                    session.execute(
                        sa_update(ShopifyProduct)
                        .where(ShopifyProduct.id == product_id)
                        .values(
                            searchQuery=new_query,
                            updatedAt=now,
                        )
                    )
                    # Vector lives in ShopifyProductEmbedding now — drop the
                    # row so the embed call below refills it cleanly.
                    invalidate_search_query_vector(session, product_id)
                    product.searchQuery = new_query

            # Cache the Vertex embedding of the effective query (override wins).
            effective_query = (product.searchQueryOverride or product.searchQuery or "").strip()
            if effective_query:
                vec = embed_text(effective_query, task_type="RETRIEVAL_DOCUMENT")
                if vec:
                    save_search_query_vector(session, product_id, vec)

            print(
                f"    [✓] semantics updated for '{product.title[:40]}' — "
                f"variants={len(updated_ids)}/{len(variants)} "
                f"searchQuery={'set' if product.searchQuery else 'missing'}"
            )

    except Exception as exc:
        raise self.retry(exc=exc)

    for variant_id in updated_ids:
        app.send_task('shopify_embedder.generate_shopify_embeddings', args=[variant_id], queue='embedding_queue')
        print(f"    [>] Queued Shopify embedding: {variant_id[:8]}")
