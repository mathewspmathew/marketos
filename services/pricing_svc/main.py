"""
services/pricing_svc/main.py

Celery task wrapper around the pure decision logic in decide.py.
"""
from __future__ import annotations

import logging

from services.common.celery_app import app
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
