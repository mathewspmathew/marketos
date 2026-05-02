"""
services/embedding_svc/main.py

Task: generate_embeddings  (embedding_queue)
  Load ScrapedProduct + variants from DB.
  Per variant: text embedding from ScrapedVariant.semanticText (Vertex AI text-embedding-004).
  Product-level: image embedding from ScrapedProduct.imageUrl (Vertex AI multimodalembedding@001).
  Write one ProductEmbedding row per variant via raw SQL (pgvector has no ORM type).
"""

import os
import uuid

import requests
import vertexai
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.orm import selectinload
from vertexai.language_models import TextEmbeddingModel
from vertexai.vision_models import Image, MultiModalEmbeddingModel

from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ScrapedProduct

load_dotenv()

VERTEX_PROJECT  = os.getenv("VERTEX_PROJECT", "marketos-494011")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")

if VERTEX_PROJECT:
    vertexai.init(project=VERTEX_PROJECT, location=VERTEX_LOCATION)

try:
    _text_model = TextEmbeddingModel.from_pretrained("text-embedding-004")
except Exception:
    _text_model = None

try:
    _image_model = MultiModalEmbeddingModel.from_pretrained("multimodalembedding@001")
except Exception:
    _image_model = None

_EMBEDDING_MODEL_TAG = "text-embedding-004+multimodalembedding@001"


# ─────────────────────────────────────────────────────────────────────────────
# Embedding helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_text_embedding(text_input: str) -> list[float] | None:
    if not text_input or not _text_model:
        return None
    try:
        result = _text_model.get_embeddings([text_input])
        return result[0].values
    except Exception as e:
        print(f"    [!] Text embedding error: {e}")
        return None


def get_image_embedding(image_url: str) -> list[float] | None:
    if not image_url or not _image_model:
        return None
    try:
        if image_url.startswith("https://storage.googleapis.com/"):
            path  = image_url.replace("https://storage.googleapis.com/", "")
            image = Image(gcs_uri=f"gs://{path}")
        else:
            image_bytes = requests.get(image_url, timeout=15).content
            image = Image(image_bytes=image_bytes)
        result = _image_model.get_embeddings(image=image, dimension=768)
        return result.image_embedding
    except Exception as e:
        print(f"    [!] Image embedding error: {e}")
        return None


def _vec(values: list[float]) -> str:
    return "[" + ",".join(str(x) for x in values) + "]"


# ─────────────────────────────────────────────────────────────────────────────
# Core embedding logic
# ─────────────────────────────────────────────────────────────────────────────

def _generate(product_id: str) -> None:
    with get_db() as session:
        product = (
            session.query(ScrapedProduct)
            .options(selectinload(ScrapedProduct.variants))
            .filter(ScrapedProduct.id == product_id)
            .first()
        )
        if not product:
            print(f"[!] Product not found: {product_id}")
            return

        print(f"[>] Embedding: {product.title[:50]} | {len(product.variants)} variant(s)")

        # Image embedding is shared across all variants (same product image)
        image_vec = get_image_embedding(product.imageUrl or "")

        # Clear stale embeddings before writing fresh ones
        session.execute(
            text('DELETE FROM "ProductEmbedding" WHERE "prodId" = :pid'),
            {"pid": product_id},
        )

        written = 0
        for v in product.variants:
            if not v.semanticText:
                print(f"    [-] No semanticText for variant {v.id[:8]} — skipping")
                continue

            text_vec = get_text_embedding(v.semanticText)
            if not text_vec:
                print(f"    [!] Text embedding failed for variant {v.id[:8]}")
                continue

            row_id = str(uuid.uuid4())
            base_params = {
                "id":        row_id,
                "userId":    product.userId,
                "prodId":    product_id,
                "variantId": v.id,
                "model":     _EMBEDDING_MODEL_TAG,
                "text_vec":  _vec(text_vec),
            }

            if image_vec:
                session.execute(
                    text(
                        'INSERT INTO "ProductEmbedding" '
                        '(id, "userId", "prodId", "variantId", '
                        '"vectorText", "vectorImg", "embeddingModel", "vectorizedAt") '
                        'VALUES (:id, :userId, :prodId, :variantId, '
                        'CAST(:text_vec AS vector), CAST(:img_vec AS vector), :model, NOW())'
                    ),
                    {**base_params, "img_vec": _vec(image_vec)},
                )
            else:
                session.execute(
                    text(
                        'INSERT INTO "ProductEmbedding" '
                        '(id, "userId", "prodId", "variantId", '
                        '"vectorText", "embeddingModel", "vectorizedAt") '
                        'VALUES (:id, :userId, :prodId, :variantId, '
                        'CAST(:text_vec AS vector), :model, NOW())'
                    ),
                    base_params,
                )
            written += 1

        print(f"[✓] Wrote {written} ProductEmbedding row(s) for: {product.title[:50]}")


# ─────────────────────────────────────────────────────────────────────────────
# Celery task
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name='embedder.generate_embeddings', bind=True, max_retries=3, default_retry_delay=60, rate_limit='10/m')
def generate_embeddings(self, product_id: str):
    try:
        _generate(product_id)
    except Exception as exc:
        print(f"    [!] Embedding failed for {product_id}: {exc} — retrying")
        raise self.retry(exc=exc)
