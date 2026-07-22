"""
services/scraper_svc/semantics.py

Task 3 — generate_variant_semantics (semantic_queue)
  One Groq call for all ScrapedVariants → bulk-update semanticText → queue embeddings.
  Uses GROQ_API_KEY_SEMANTIC (separate key for TPM isolation from extraction, which
  shares GROQ_API_KEY with extract_product/extract_candidate).

Task 4 — generate_shopify_variant_semantics (shopify_semantic_queue)
  Same flow for ShopifyVariants. Triggered by the API gateway on product create/update.
  Uses GROQ_API_KEY_SHOPIFY (separate key for TPM isolation from competitor scraping).
"""

import json
import os
from datetime import datetime, timezone, timedelta

import structlog
from celery.exceptions import Retry as CeleryRetry
from dotenv import load_dotenv
from litellm.exceptions import RateLimitError as GroqRateLimitError
from sqlalchemy import text, text as sa_text, update as sa_update

from services.common.celery_app import app
from services.common.db import get_db
from services.common.groq_client import semantic_router, shopify_semantic_router
from services.common.models import ScrapedProduct, ScrapedVariant, ShopifyProduct, ShopifyVariant
from services.scraper_svc.helpers import log_error

load_dotenv()

logger = structlog.get_logger(__name__)

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
    router = shopify_semantic_router if shopify else semantic_router
    response = router.completion(
        model="groq",
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
    router = shopify_semantic_router if shopify else semantic_router
    prompt = GROQ_SEARCH_QUERY_PROMPT.format(
        title=title or "",
        vendor=vendor or "Unknown Brand",
        category=category or "Product",
        description=(description or "")[:400],
    )
    try:
        response = router.completion(
            model="groq",
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
    except Exception:
        logger.exception("search_query_generation_failed", title=title)
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
# Claim + CAS helpers for the ShopifyProduct semantic state machine
# ─────────────────────────────────────────────────────────────────────────────

_STALE_QUEUED_MINUTES = 10
_BACKFILL_BATCH = 100


def claim_and_enqueue_semantics(session, *, ids=None, limit=_BACKFILL_BATCH):
    """Atomically claim PENDING (ids path) or PENDING+stale-QUEUED (beat path)
    ShopifyProducts via PENDING/stale -> QUEUED, then enqueue one semantic task
    per claimed product. Returns the list of claimed product ids."""
    if ids is not None:
        if not ids:
            return []
        rows = session.execute(text('''
            UPDATE "ShopifyProduct"
               SET "semanticStatus"='QUEUED', "semanticClaimedAt"=NOW(),
                   "semanticAttempts"="semanticAttempts"+1
             WHERE id = ANY(:ids) AND "semanticStatus"='PENDING'
            RETURNING id
        '''), {"ids": list(ids)}).all()
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=_STALE_QUEUED_MINUTES)
        rows = session.execute(text('''
            UPDATE "ShopifyProduct"
               SET "semanticStatus"='QUEUED', "semanticClaimedAt"=NOW(),
                   "semanticAttempts"="semanticAttempts"+1
             WHERE id IN (
               SELECT id FROM "ShopifyProduct"
                WHERE "semanticStatus"='PENDING'
                   OR ("semanticStatus"='QUEUED' AND "semanticClaimedAt" < :cutoff)
                ORDER BY "semanticClaimedAt" NULLS FIRST
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
             )
            RETURNING id
        '''), {"cutoff": cutoff, "limit": limit}).all()
    claimed = [r.id for r in rows]
    session.commit()
    for pid in claimed:
        app.send_task('scraper.generate_shopify_variant_semantics', args=[pid],
                      queue='shopify_semantic_queue')
    return claimed


def _finalize_semantic_done(session, product_id, version_read) -> int:
    """CAS: mark DONE + clear claim only if version unchanged. Returns rowcount."""
    return session.execute(text('''
        UPDATE "ShopifyProduct"
           SET "semanticStatus"='DONE', "semanticClaimedAt"=NULL
         WHERE id=:id AND "semanticVersion"=:v
    '''), {"id": product_id, "v": version_read}).rowcount


def _mark_semantic_failed(product_id, version_read, reason: str) -> None:
    """CAS: mark FAILED if version unchanged (else a newer task owns it)."""
    with get_db() as session:
        session.execute(text('''
            UPDATE "ShopifyProduct"
               SET "semanticStatus"='FAILED', "semanticClaimedAt"=NULL,
                   "semanticFailureReason"=:r
             WHERE id=:id AND "semanticVersion"=:v
        '''), {"id": product_id, "v": version_read, "r": reason[:500]})
        session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Task 3: generate_variant_semantics
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name='scraper.generate_variant_semantics', bind=True, max_retries=3, default_retry_delay=30, rate_limit='3/m')
def generate_variant_semantics(self, product_id: str, config_id: str, shop_domain: str, product_url: str):
    """One Groq call generates semanticText for all ScrapedVariants, then queues embeddings."""
    logger.info("generating_variant_semantics", product_id=product_id)

    try:
        with get_db() as session:
            product  = session.query(ScrapedProduct).filter(ScrapedProduct.id == product_id).first()
            variants = session.query(ScrapedVariant).filter(ScrapedVariant.productId == product_id).all()

            if not product:
                logger.warning("product_not_found", product_id=product_id)
                return
            if not variants:
                logger.warning("no_variants_for_product", product_id=product_id)
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

            logger.info(
                "semantic_text_written",
                updated_count=updated,
                total_count=len(variants),
                product_title=product.title,
            )

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
        logger.info("embedding_queued", product_id=product_id)
    except Exception as e:
        logger.exception("embedding_queue_failed_retrying", product_id=product_id)
        raise self.retry(exc=e)


# ─────────────────────────────────────────────────────────────────────────────
# Task 4: generate_shopify_variant_semantics
# ─────────────────────────────────────────────────────────────────────────────

def _short_id(gid: str) -> str:
    """Extract the numeric tail from a Shopify GID so the LLM gets a simple key."""
    return gid.rsplit("/", 1)[-1]


@app.task(name='scraper.generate_shopify_variant_semantics', bind=True,
          max_retries=3, default_retry_delay=30, rate_limit='3/m')
def generate_shopify_variant_semantics(self, product_id: str):
    """Generate semanticText + searchQuery for a product, fenced by semanticVersion.
    Reads version at load; CAS on write so a concurrent edit can't be clobbered."""
    # READ PHASE (short txn)
    with get_db() as session:
        product = session.query(ShopifyProduct).filter(ShopifyProduct.id == product_id).first()
        if not product:
            return
        version_read = product.semanticVersion
        variants = session.query(ShopifyVariant).filter(
            ShopifyVariant.productId == product_id,
            ShopifyVariant.semanticText == None,  # noqa: E711
        ).all()
        needs_search_query = not product.searchQuery and not product.searchQueryOverride
        if not variants and not needs_search_query:
            _finalize_semantic_done(session, product_id, version_read)
            session.commit()
            return
        p_title, p_vendor, p_type = product.title, product.vendor, product.productType
        p_desc, p_tags = product.description, product.tags
        p_search_override, p_search = product.searchQueryOverride, product.searchQuery
        p_category = product.categoryTop
        id_map = {_short_id(v.id): v.id for v in variants}
        variants_payload = [{
            "id": _short_id(v.id), "title": v.title, "options": v.options or {},
            "current_price": float(v.currentPrice or 0),
            "original_price": float(v.compareAtPrice) if v.compareAtPrice else None,
            "is_in_stock": v.isInStock,
        } for v in variants]

    # GENERATE PHASE (no DB held)
    semantic_map = {}
    if variants_payload:
        try:
            semantic_map = _groq_semantic_call(
                _build_semantic_prompt(p_title, p_vendor, p_type, p_desc, p_tags, None,
                                       variants_payload), shopify=True)
        except GroqRateLimitError:
            raise self.retry(countdown=65)
        except Exception as e:
            if self.request.retries >= self.max_retries:
                _mark_semantic_failed(product_id, version_read, str(e))
                return
            raise self.retry(exc=e)

    parsed = []
    var_texts = {}
    for short_key, full_id in id_map.items():
        t = semantic_map.get(short_key, "")
        if t:
            var_texts[full_id] = t
            parsed.append(_parse_fingerprint_fields(t))

    # Don't mark DONE on a partial/empty Groq result — that would strand the
    # un-returned variants un-embedded (DONE removes them from backfill).
    missing = set(id_map.values()) - set(var_texts.keys())
    if missing:
        reason = f"semantic generation incomplete: {len(missing)}/{len(id_map)} variant(s) missing"
        if self.request.retries >= self.max_retries:
            _mark_semantic_failed(product_id, version_read, reason)
            return
        raise self.retry(countdown=30)

    product_fields = _consolidate_product_fields(parsed) if parsed else {}
    category_for_query = (product_fields.get("categoryTop") if product_fields else None) or p_category or p_type

    new_query = None
    if not p_search_override and not p_search:
        try:
            new_query = _groq_search_query(title=p_title, vendor=p_vendor,
                                           category=category_for_query, description=p_desc)
        except Exception as e:
            if self.request.retries >= self.max_retries:
                _mark_semantic_failed(product_id, version_read, str(e))
                return
            raise self.retry(exc=e)

    # WRITE PHASE (CAS-fenced txn)
    # Order matters: write variants first, then CAS on ShopifyProduct (which
    # acquires the row lock). Only if the CAS succeeds do we write the
    # ShopifyProduct-level fields (searchQuery, categoryTop, productGender) —
    # keeping them after the CAS avoids a lock-order conflict where a concurrent
    # version bump tries to UPDATE ShopifyProduct while we already hold its lock.
    now = datetime.now(timezone.utc)
    with get_db() as session:
        for full_id, t in var_texts.items():
            session.execute(sa_update(ShopifyVariant)
                            .where(ShopifyVariant.id == full_id)
                            .values(semanticText=t, updatedAt=now))
        rc = _finalize_semantic_done(session, product_id, version_read)
        if rc == 0:
            session.rollback()
            logger.info("semantic_result_discarded_stale", product_id=product_id)
            return
        # CAS succeeded — safe to write ShopifyProduct fields in the same txn
        if product_fields.get("categoryTop") or product_fields.get("productGender"):
            session.execute(sa_update(ShopifyProduct).where(ShopifyProduct.id == product_id)
                            .values(categoryTop=product_fields.get("categoryTop"),
                                    productGender=product_fields.get("productGender"), updatedAt=now))
        if new_query:
            session.execute(sa_update(ShopifyProduct).where(ShopifyProduct.id == product_id)
                            .values(searchQuery=new_query, updatedAt=now))
        session.commit()

    for full_id in var_texts:
        app.send_task('shopify_embedder.generate_shopify_embeddings', args=[full_id], queue='embedding_queue')
