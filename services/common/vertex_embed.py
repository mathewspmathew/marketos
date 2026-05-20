"""
services/common/vertex_embed.py

Shared Vertex AI text-embedding helper for any worker that needs to embed
short ad-hoc strings (e.g. SERP snippets for discovery rerank).

The competitor embedding worker (services/embedding_svc/main.py) has its own
inline copy of this for historical reasons; that will be migrated over.
Both call the same model so vectors are directly comparable.
"""
from __future__ import annotations

import os
from typing import Iterable

from google import genai
from google.genai.types import EmbedContentConfig

VERTEX_PROJECT  = os.getenv("VERTEX_PROJECT", "marketos-494011")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")

TEXT_MODEL     = "text-embedding-004"
TEXT_DIMS      = 768
BATCH_LIMIT    = 250  # Vertex hard cap per request

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(
            vertexai=True, project=VERTEX_PROJECT, location=VERTEX_LOCATION,
        )
    return _client


def embed_text(text: str, *, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float] | None:
    """Embed a single string. Returns None on empty input or failure."""
    if not text:
        return None
    vecs = embed_texts([text], task_type=task_type)
    return vecs[0] if vecs else None


def embed_texts(
    texts: Iterable[str],
    *,
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> list[list[float] | None]:
    """Batch-embed strings. Preserves input order; bad/empty entries get None.

    Splits into chunks of BATCH_LIMIT so callers don't have to think about it.
    """
    items = list(texts)
    if not items:
        return []

    # Mark blanks so we don't waste API budget on them.
    nonblank_idx = [i for i, t in enumerate(items) if t and t.strip()]
    if not nonblank_idx:
        return [None] * len(items)

    out: list[list[float] | None] = [None] * len(items)
    client = _get_client()

    cfg = EmbedContentConfig(
        output_dimensionality=TEXT_DIMS,
        task_type=task_type,
    )

    for chunk_start in range(0, len(nonblank_idx), BATCH_LIMIT):
        chunk_idx = nonblank_idx[chunk_start : chunk_start + BATCH_LIMIT]
        chunk_texts = [items[i] for i in chunk_idx]
        try:
            result = client.models.embed_content(
                model=TEXT_MODEL,
                contents=chunk_texts,
                config=cfg,
            )
            for slot, emb in zip(chunk_idx, result.embeddings):
                out[slot] = list(emb.values)
        except Exception as exc:
            # Leave None for this chunk; caller decides how to handle.
            print(f"[vertex_embed] batch failed ({len(chunk_idx)} items): {exc}")

    return out


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Assumes neither vector is zero."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ─────────────────────────────────────────────────────────────────────────────
# ShopifyProduct.searchQueryVector — pgvector accessors.
# pgvector columns aren't in the SQLAlchemy ORM (Unsupported in Prisma); these
# helpers use raw SQL so callers don't have to think about the encoding.
# ─────────────────────────────────────────────────────────────────────────────
def _vec_literal(vec: list[float]) -> str:
    """Format a Python float list as a pgvector literal: '[1.0,2.0,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def load_search_query_vector(db, shopify_product_id: str) -> list[float] | None:
    """Return the cached ShopifyProductEmbedding.searchQueryVector, or None."""
    from sqlalchemy import text
    row = db.execute(
        text(
            'SELECT "searchQueryVector"::text '
            'FROM "ShopifyProductEmbedding" WHERE "productId" = :id'
        ),
        {"id": shopify_product_id},
    ).first()
    if not row or row[0] is None:
        return None
    raw = row[0].strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return None
    try:
        return [float(x) for x in raw[1:-1].split(",") if x]
    except ValueError:
        return None


def save_search_query_vector(
    db,
    shopify_product_id: str,
    vec: list[float],
    *,
    shop_domain: str | None = None,
) -> None:
    """Upsert the searchQueryVector for a product into ShopifyProductEmbedding.

    Looks up shopDomain when not provided. Safe to call repeatedly.
    """
    from sqlalchemy import text
    if not vec:
        return

    if shop_domain is None:
        sd_row = db.execute(
            text('SELECT "shopDomain" FROM "ShopifyProduct" WHERE id = :id'),
            {"id": shopify_product_id},
        ).first()
        if not sd_row:
            return
        shop_domain = sd_row[0]

    db.execute(
        text("""
            INSERT INTO "ShopifyProductEmbedding"
                ("productId", "shopDomain", "searchQueryVector", "embeddedAt", "updatedAt")
            VALUES (:id, :sd, CAST(:v AS vector), NOW(), NOW())
            ON CONFLICT ("productId") DO UPDATE
              SET "searchQueryVector" = CAST(:v AS vector),
                  "updatedAt"         = NOW()
        """),
        {"id": shopify_product_id, "sd": shop_domain, "v": _vec_literal(vec)},
    )


def invalidate_search_query_vector(db, shopify_product_id: str) -> None:
    """Drop the cached vector so the next embed call regenerates fresh."""
    from sqlalchemy import text
    db.execute(
        text('DELETE FROM "ShopifyProductEmbedding" WHERE "productId" = :id'),
        {"id": shopify_product_id},
    )
