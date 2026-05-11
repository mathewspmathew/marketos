"""
services/embedding_svc/main.py

Task: generate_embeddings  (embedding_queue)
  Load ScrapedProduct + variants from DB.
  Per variant: text embedding from ScrapedVariant.semanticText (Vertex AI text-embedding-004).
  Product-level: image embedding from ScrapedProduct.imageUrl (Vertex AI multimodalembedding@001).
  Write one ProductEmbedding row per variant via raw SQL (pgvector has no ORM type).
"""

import base64
import os
import uuid

import requests
from dotenv import load_dotenv
from google import genai
from google.cloud import aiplatform_v1
from google.genai.types import EmbedContentConfig
from sqlalchemy import text
from sqlalchemy.orm import selectinload

from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ScrapedProduct, ShopifyProduct, ShopifyVariant

load_dotenv()

VERTEX_PROJECT  = os.getenv("VERTEX_PROJECT", "marketos-494011")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")

# google-genai client backed by Vertex AI for text embeddings.
try:
    _genai_client = genai.Client(
        vertexai=True, project=VERTEX_PROJECT, location=VERTEX_LOCATION,
    )
except Exception as e:
    print(f"[!] google-genai init failed: {e}")
    _genai_client = None

# Vertex AI prediction client for the multimodal image embedding model.
# google-genai does not expose multimodalembedding@001 directly; the underlying
# PredictionServiceClient is the non-deprecated path.
try:
    _predict_client = aiplatform_v1.PredictionServiceClient(
        client_options={"api_endpoint": f"{VERTEX_LOCATION}-aiplatform.googleapis.com"}
    )
    _image_endpoint = (
        f"projects/{VERTEX_PROJECT}/locations/{VERTEX_LOCATION}"
        f"/publishers/google/models/multimodalembedding@001"
    )
except Exception as e:
    print(f"[!] aiplatform PredictionServiceClient init failed: {e}")
    _predict_client = None
    _image_endpoint = None

_EMBEDDING_MODEL_TAG = "text-embedding-004+multimodalembedding@001"
_TEXT_EMBED_DIMENSIONS = 768


# ─────────────────────────────────────────────────────────────────────────────
# Embedding helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_text_embedding(text_input: str) -> list[float] | None:
    if not text_input or not _genai_client:
        return None
    try:
        result = _genai_client.models.embed_content(
            model="text-embedding-004",
            contents=[text_input],
            config=EmbedContentConfig(
                output_dimensionality=_TEXT_EMBED_DIMENSIONS,
                task_type="RETRIEVAL_DOCUMENT",
            ),
        )
        return list(result.embeddings[0].values)
    except Exception as e:
        print(f"    [!] Text embedding error: {e}")
        return None


def get_image_embedding(image_url: str) -> list[float] | None:
    if not image_url or not _predict_client or not _image_endpoint:
        return None
    try:
        if image_url.startswith("https://storage.googleapis.com/"):
            gcs_uri = "gs://" + image_url.replace("https://storage.googleapis.com/", "")
            image_payload = {"gcsUri": gcs_uri}
        else:
            image_bytes = requests.get(image_url, timeout=15).content
            image_payload = {"bytesBase64Encoded": base64.b64encode(image_bytes).decode("ascii")}

        response = _predict_client.predict(
            endpoint=_image_endpoint,
            instances=[{"image": image_payload}],
            parameters={"dimension": 768},
        )
        if not response.predictions:
            return None
        # PredictionServiceClient returns proto Struct values — convert to dict.
        prediction = dict(response.predictions[0])
        embedding = prediction.get("imageEmbedding")
        return list(embedding) if embedding else None
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
        print(f"[✓] Wrote {written}/{eligible} ProductEmbedding row(s) for: {product.title[:50]}")
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
        print(f"    [!] Embedding failed for {product_id}: {exc} — retrying")
        raise self.retry(exc=exc)

    # Fresh competitor embeddings → re-match only what's actually dirty.
    # The competitor PEs we just wrote carry matchedAt=NULL, so the dirty
    # selector in match_for_shop picks up the affected merchant variants.
    with get_db() as session:
        prod = session.query(ScrapedProduct).filter(ScrapedProduct.id == product_id).first()
        if prod:
            app.send_task(
                "matcher.match_for_shop",
                args=[prod.shopDomain, False],
                queue="match_queue",
            )


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
            print(f"[!] ShopifyVariant not found: {variant_id}")
            return

        if not variant.semanticText:
            print(f"[-] No semanticText for ShopifyVariant {variant_id[:8]} — skipping")
            return

        print(f"[>] Shopify embedding: variant {variant_id[:8]}")

        text_vec = get_text_embedding(variant.semanticText)
        if not text_vec:
            print(f"    [!] Text embedding failed for ShopifyVariant {variant_id[:8]}")
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

        print(f"[✓] ShopifyEmbedding written for variant {variant_id[:8]}")


@app.task(name='shopify_embedder.generate_shopify_embeddings', bind=True, max_retries=3, default_retry_delay=60, rate_limit='10/m')
def generate_shopify_embeddings(self, variant_id: str):
    try:
        _generate_shopify(variant_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            print(f"    [!] Shopify embedding permanently failed for {variant_id}: {exc}", flush=True)
            return
        print(f"    [!] Shopify embedding failed for {variant_id}: {exc} — retrying")
        raise self.retry(exc=exc)

    # Fresh merchant embedding → match this variant against all competitor domains.
    with get_db() as session:
        variant = (
            session.query(ShopifyVariant)
            .options(selectinload(ShopifyVariant.product))
            .filter(ShopifyVariant.id == variant_id)
            .first()
        )
        if variant and variant.product:
            app.send_task(
                "matcher.match_for_variant",
                args=[variant.product.shopDomain, variant_id],
                queue="match_queue",
            )
