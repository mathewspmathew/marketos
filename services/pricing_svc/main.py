"""
services/pricing_svc/main.py

Celery task wrappers around the pure decision logic in decide.py.

v1 exposes one task — pricing.decide_for_product — fanned out from
stats.recompute_for_variant after a fresh observation lands. The legacy
pricing.decide_for_variant task is kept as a thin shim that resolves the
variant to its product and delegates, so old enqueues during the rollout
don't error out.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.pricing_svc.decide import decide_price_for_product

logger = logging.getLogger(__name__)


@app.task(name="pricing.decide_for_product", bind=True, max_retries=3, default_retry_delay=15)
def decide_for_product(self, shop_domain: str, shopify_product_id: str):
    try:
        return decide_price_for_product(shop_domain, shopify_product_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error("decide_for_product %s permanently failed: %s", shopify_product_id, exc)
            return {"ok": False, "reason": "exception", "error": str(exc)}
        raise self.retry(exc=exc)


@app.task(name="pricing.decide_for_variant", bind=True, max_retries=3, default_retry_delay=15)
def decide_for_variant(self, shop_domain: str, shopify_variant_id: str):
    """Legacy entry point. Resolves variant → product and delegates."""
    try:
        with get_db() as session:
            row = session.execute(
                text('SELECT "productId" FROM "ShopifyVariant" WHERE id = :vid'),
                {"vid": shopify_variant_id},
            ).first()
        if not row:
            return {"ok": False, "reason": "variant_missing"}
        return decide_price_for_product(shop_domain, row[0])
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error("decide_for_variant %s permanently failed: %s", shopify_variant_id, exc)
            return {"ok": False, "reason": "exception", "error": str(exc)}
        raise self.retry(exc=exc)
