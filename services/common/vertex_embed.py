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

import structlog
from google import genai
from google.genai.types import EmbedContentConfig

logger = structlog.get_logger(__name__)

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
        except Exception:
            # Leave None for this chunk; caller decides how to handle.
            logger.exception("vertex_embed_batch_failed", chunk_size=len(chunk_idx), task_type=task_type)

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


