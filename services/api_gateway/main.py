"""
services/api_gateway/main.py

Internal HTTP API consumed by the Shopify frontend (React Router webhooks).
Not exposed publicly — only reachable within the Docker network.

Run: uvicorn services.api_gateway.main:app --host 0.0.0.0 --port 8000
"""

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from sqlalchemy import distinct

from services.common.celery_app import app as celery_app
from services.common.db import get_db
from services.common.models import ShopifyVariant

load_dotenv()

app = FastAPI(title="MarketOS Internal API", docs_url=None, redoc_url=None)


@app.post("/internal/shopify/product-updated")
def shopify_product_updated(product_id: str):
    """
    Triggered by webhooks.products.create / webhooks.products.update after DB upsert.
    Resets semanticText on all variants then queues semantic generation.
    """
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id is required")

    celery_app.send_task(
        'scraper.generate_shopify_variant_semantics',
        args=[product_id],
        queue='semantic_queue',
    )
    return {"queued": True, "product_id": product_id}


@app.post("/internal/shopify/backfill-semantics")
def backfill_shopify_semantics():
    """
    One-shot trigger: queue semantic generation for every ShopifyProduct
    that has at least one variant with semanticText still null.
    Safe to call multiple times — the task itself skips variants that already have text.
    """
    with get_db() as session:
        rows = (
            session.query(distinct(ShopifyVariant.productId))
            .filter(ShopifyVariant.semanticText == None)  # noqa: E711
            .all()
        )

    product_ids = [row[0] for row in rows]
    for product_id in product_ids:
        celery_app.send_task(
            'scraper.generate_shopify_variant_semantics',
            args=[product_id],
            queue='semantic_queue',
        )

    return {"queued": len(product_ids), "product_ids": product_ids}


@app.get("/health")
def health():
    return {"status": "ok"}
