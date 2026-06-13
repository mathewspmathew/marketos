"""
services/api_gateway/main.py

Internal HTTP API consumed by the Shopify frontend (React Router webhooks).
Not exposed publicly — only reachable within the Docker network.

Run: uvicorn services.api_gateway.main:app --host 0.0.0.0 --port 8000
"""

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from services.common.celery_app import app as celery_app
from services.common.db import get_db
from services.scraper_svc.semantics import claim_and_enqueue_semantics

load_dotenv()

app = FastAPI(title="MarketOS Internal API", docs_url=None, redoc_url=None)


@app.post("/internal/shopify/product-updated")
def shopify_product_updated(product_id: str):
    """Triggered by products/create / products/update webhooks after DB upsert.
    Claims the product (PENDING -> QUEUED) and enqueues one semantic task."""
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id is required")
    with get_db() as session:
        claim_and_enqueue_semantics(session, ids=[product_id])
    return {"queued": True, "product_id": product_id}


@app.post("/internal/shopify/retry-failed-semantics")
def retry_failed_semantics(shop_domain: str):
    """Reset all FAILED products for a shop back to PENDING (+ bump version) so the
    next beat tick re-claims them."""
    if not shop_domain:
        raise HTTPException(status_code=422, detail="shop_domain is required")
    with get_db() as session:
        rc = session.execute(text('''
            UPDATE "ShopifyProduct"
               SET "semanticStatus"='PENDING', "semanticVersion"="semanticVersion"+1,
                   "semanticFailureReason"=NULL
             WHERE "shopDomain"=:sd AND "semanticStatus"='FAILED'
        '''), {"sd": shop_domain}).rowcount
        session.commit()
    return {"ok": True, "reset": rc}


@app.post("/internal/shopify/backfill-semantics")
def backfill_shopify_semantics():
    """Manual kick of the semantic backfill — same bounded claim the beat uses.
    Claims PENDING/stale-QUEUED products (at most the claim's batch limit) and
    enqueues one task each; never bypasses the claim."""
    with get_db() as session:
        claimed = claim_and_enqueue_semantics(session, ids=None)
    return {"queued": len(claimed), "product_ids": claimed}


@app.post("/internal/suggestion/regenerate")
def regenerate_suggestions(shop_domain: str, scope: str = "first_time_and_showed"):
    """Triggered by the Suggestions UI 'Re-suggest' button. Fans out per-product tasks."""
    if not shop_domain:
        raise HTTPException(status_code=422, detail="shop_domain is required")
    if scope not in ("all", "first_time_and_showed"):
        raise HTTPException(status_code=422, detail="invalid scope")

    celery_app.send_task(
        'suggestion.suggest_for_shop',
        args=[shop_domain, scope],
        queue='suggestion_queue',
    )
    return {"queued": True, "shop_domain": shop_domain, "scope": scope}


@app.post("/internal/suggestion/regenerate-product")
def regenerate_suggestion_for_product(shop_domain: str, product_id: str):
    """Regenerate suggestions for a single product (used after Apply or on-demand)."""
    if not shop_domain or not product_id:
        raise HTTPException(status_code=422, detail="shop_domain and product_id are required")

    celery_app.send_task(
        'suggestion.suggest_for_product',
        args=[shop_domain, product_id],
        queue='suggestion_queue',
    )
    return {"queued": True, "shop_domain": shop_domain, "product_id": product_id}


@app.post("/internal/shopify/sync-products")
def shopify_sync_products(shop_domain: str):
    """Triggered by the products page (Refresh button / loader auto-kick).
    Enqueues a durable full product pull from Shopify into Postgres."""
    if not shop_domain:
        raise HTTPException(status_code=422, detail="shop_domain is required")

    celery_app.send_task(
        "shopify_sync.pull_products",
        args=[shop_domain],
        queue="shopify_sync_queue",
    )
    return {"queued": True, "shop_domain": shop_domain}


@app.get("/health")
def health():
    return {"status": "ok"}
