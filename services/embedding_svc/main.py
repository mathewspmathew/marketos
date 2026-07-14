"""
services/embedding_svc/main.py

Task: generate_embeddings  (embedding_queue)
  Load ScrapedProduct + variants from DB.
  Per variant: text embedding from ScrapedVariant.semanticText (Vertex AI text-embedding-004).
  Product-level: image embedding from ScrapedProduct.imageUrl (Vertex AI multimodalembedding@001).
  Write one ProductEmbedding row per variant via raw SQL (pgvector has no ORM type).
"""

import base64
import io
import os
import uuid

import requests
import structlog
from dotenv import load_dotenv
from google import genai
from google.cloud import aiplatform_v1
from google.genai.types import EmbedContentConfig
from PIL import Image
from sqlalchemy import text
from sqlalchemy.orm import selectinload

from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ScrapedProduct, ShopifyProduct, ShopifyVariant

load_dotenv()

logger = structlog.get_logger(__name__)

VERTEX_PROJECT  = os.getenv("VERTEX_PROJECT", "marketos-494011")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")

# Vertex clients are lazy-initialized so workers that don't run embedding tasks
# (but still autodiscover this module) don't need GCP credentials at boot.
_genai_client = None
_predict_client = None
_image_endpoint = None


def _get_genai_client():
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(
            vertexai=True, project=VERTEX_PROJECT, location=VERTEX_LOCATION,
        )
    return _genai_client


def _get_predict_client():
    global _predict_client, _image_endpoint
    if _predict_client is None:
        _predict_client = aiplatform_v1.PredictionServiceClient(
            client_options={"api_endpoint": f"{VERTEX_LOCATION}-aiplatform.googleapis.com"}
        )
        _image_endpoint = (
            f"projects/{VERTEX_PROJECT}/locations/{VERTEX_LOCATION}"
            f"/publishers/google/models/multimodalembedding@001"
        )
    return _predict_client, _image_endpoint

_EMBEDDING_MODEL_TAG = "text-embedding-004+multimodalembedding@001"
_TEXT_EMBED_DIMENSIONS = 768
_IMAGE_EMBED_DIMENSIONS = 1408  # multimodalembedding@001 supports 128/256/512/1408 only


# ─────────────────────────────────────────────────────────────────────────────
# Embedding helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_text_embedding(text_input: str) -> list[float] | None:
    if not text_input:
        return None
    try:
        client = _get_genai_client()
        result = client.models.embed_content(
            model="text-embedding-004",
            contents=[text_input],
            config=EmbedContentConfig(
                output_dimensionality=_TEXT_EMBED_DIMENSIONS,
                task_type="RETRIEVAL_DOCUMENT",
            ),
        )
        return list(result.embeddings[0].values)
    except Exception:
        logger.exception("text_embedding_failed")
        return None


def get_image_embedding(image_url: str) -> list[float] | None:
    if not image_url:
        return None
    try:
        predict_client, image_endpoint = _get_predict_client()
        # Always download and normalize to JPEG. multimodalembedding@001 rejects
        # WEBP and other formats, and URL extensions can't be trusted — scraped
        # images often end in .jpeg but are actually WEBP.
        raw = requests.get(image_url, timeout=15).content
        img = Image.open(io.BytesIO(raw))
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        image_payload = {"bytesBase64Encoded": base64.b64encode(buf.getvalue()).decode("ascii")}

        response = predict_client.predict(
            endpoint=image_endpoint,
            instances=[{"image": image_payload}],
            parameters={"dimension": _IMAGE_EMBED_DIMENSIONS},
        )
        if not response.predictions:
            return None
        # PredictionServiceClient returns proto Struct values — convert to dict.
        prediction = dict(response.predictions[0])
        embedding = prediction.get("imageEmbedding")
        return list(embedding) if embedding else None
    except Exception:
        logger.exception("image_embedding_failed", image_url=image_url)
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
            logger.warning("product_not_found", product_id=product_id)
            return

        logger.info("embedding_started", title=product.title[:50], variant_count=len(product.variants))

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
                logger.info("variant_missing_semantic_text", variant_id=v.id)
                continue

            text_vec = get_text_embedding(v.semanticText)
            if not text_vec:
                logger.warning("variant_text_embedding_failed", variant_id=v.id)
                continue

            row_id = str(uuid.uuid4())
            base_params = {
                "id":         row_id,
                "shopDomain": product.shopDomain,
                "prodId":     product_id,
                "variantId":  v.id,
                "text_vec":   _vec(text_vec),
            }

            if image_vec:
                session.execute(
                    text(
                        'INSERT INTO "ProductEmbedding" '
                        '(id, "shopDomain", "prodId", "variantId", '
                        '"vectorText", "vectorImg", "vectorizedAt") '
                        'VALUES (:id, :shopDomain, :prodId, :variantId, '
                        'CAST(:text_vec AS vector), CAST(:img_vec AS vector), NOW())'
                    ),
                    {**base_params, "img_vec": _vec(image_vec)},
                )
            else:
                session.execute(
                    text(
                        'INSERT INTO "ProductEmbedding" '
                        '(id, "shopDomain", "prodId", "variantId", '
                        '"vectorText", "vectorizedAt") '
                        'VALUES (:id, :shopDomain, :prodId, :variantId, '
                        'CAST(:text_vec AS vector), NOW())'
                    ),
                    base_params,
                )
            written += 1

        eligible = sum(1 for v in product.variants if v.semanticText)
        logger.info("product_embedding_written", written=written, eligible=eligible, title=product.title[:50])
        if eligible > 0 and written == 0:
            raise RuntimeError(f"All {eligible} embedding(s) failed for product {product_id} — check Vertex AI credentials")


# ─────────────────────────────────────────────────────────────────────────────
# Celery task
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name='embedder.generate_embeddings', bind=True, max_retries=3, default_retry_delay=60, rate_limit='10/m')
def generate_embeddings(self, product_id: str):
    try:
        _generate(product_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.exception("embedding_generation_permanently_failed", product_id=product_id)
            return
        logger.exception("embedding_generation_failed_retrying", product_id=product_id)
        raise self.retry(exc=exc)

    # Fresh competitor embeddings → scoped match against the merchant
    # product(s) that triggered this scrape (resolved via CompetitorCandidate
    # inside the matcher task).
    try:
        app.send_task(
            "matcher.match_for_scraped_product",
            args=[product_id],
            queue="match_queue",
        )
    except Exception:
        logger.exception("match_dispatch_failed", product_id=product_id)


# ─────────────────────────────────────────────────────────────────────────────
# Shopify variant embedding
# ─────────────────────────────────────────────────────────────────────────────

def _generate_shopify(variant_id: str) -> None:
    with get_db() as session:
        variant = (
            session.query(ShopifyVariant)
            .options(selectinload(ShopifyVariant.product))
            .filter(ShopifyVariant.id == variant_id)
            .first()
        )
        if not variant:
            logger.warning("shopify_variant_not_found", variant_id=variant_id)
            return

        if not variant.semanticText:
            logger.info("shopify_variant_missing_semantic_text", variant_id=variant_id)
            return

        logger.info("shopify_embedding_started", variant_id=variant_id)

        text_vec = get_text_embedding(variant.semanticText)
        if not text_vec:
            logger.warning("shopify_variant_text_embedding_failed", variant_id=variant_id)
            return

        image_vec = get_image_embedding(variant.product.imageUrl or "") if variant.product else None

        row_id = str(uuid.uuid4())
        base_params = {
            "id":         row_id,
            "variantId":  variant_id,
            "shopDomain": variant.product.shopDomain,
            "text_vec":   _vec(text_vec),
        }

        if image_vec:
            session.execute(
                text(
                    'INSERT INTO "ShopifyEmbedding" '
                    '(id, "variantId", "shopDomain", "vectorText", "vectorImg", "embeddedAt", "updatedAt") '
                    'VALUES (:id, :variantId, :shopDomain, CAST(:text_vec AS vector), CAST(:img_vec AS vector), NOW(), NOW()) '
                    'ON CONFLICT ("variantId") DO UPDATE SET '
                    '"shopDomain" = EXCLUDED."shopDomain", '
                    '"vectorText" = EXCLUDED."vectorText", '
                    '"vectorImg" = EXCLUDED."vectorImg", '
                    '"updatedAt" = NOW()'
                ),
                {**base_params, "img_vec": _vec(image_vec)},
            )
        else:
            session.execute(
                text(
                    'INSERT INTO "ShopifyEmbedding" '
                    '(id, "variantId", "shopDomain", "vectorText", "embeddedAt", "updatedAt") '
                    'VALUES (:id, :variantId, :shopDomain, CAST(:text_vec AS vector), NOW(), NOW()) '
                    'ON CONFLICT ("variantId") DO UPDATE SET '
                    '"shopDomain" = EXCLUDED."shopDomain", '
                    '"vectorText" = EXCLUDED."vectorText", '
                    '"updatedAt" = NOW()'
                ),
                base_params,
            )

        logger.info("shopify_embedding_written", variant_id=variant_id)


@app.task(name='shopify_embedder.generate_shopify_embeddings', bind=True, max_retries=3, default_retry_delay=60, rate_limit='10/m')
def generate_shopify_embeddings(self, variant_id: str):
    try:
        _generate_shopify(variant_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.exception("shopify_embedding_permanently_failed", variant_id=variant_id)
            return
        logger.exception("shopify_embedding_failed_retrying", variant_id=variant_id)
        raise self.retry(exc=exc)

    # Merchant embedding writes no longer trigger the matcher. Matching is
    # scrape-driven now: when a competitor scrape lands for this merchant
    # product, matcher.match_for_scraped_product reads this fresh embedding.
