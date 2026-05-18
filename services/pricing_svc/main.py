"""
services/pricing_svc/main.py

Thin Celery task wrapper. The real logic lives in:
  - decide.py — pure decision math (testable without a database)
  - apply.py  — DB I/O + dispatch to shopify_writer

Keeping this file small means the queue surface is obvious at a glance.
"""
from __future__ import annotations

from services.common.celery_app import app
from services.pricing_svc.apply import decide_price

# after rebuilding VariantCompetitorStats, if rebuilding occurs - it is comes here

# app.send_task(
#       "pricing.decide_for_variant",
#       args=["acme-shoes.myshopify.com", "shop-variant-42"],
#       queue="pricing_queue",

@app.task(name="pricing.decide_for_variant", bind=True, max_retries=3, default_retry_delay=15)
def decide_for_variant(self, shop_domain: str, shopify_variant_id: str):
    try:
        
        return decide_price(shop_domain, shopify_variant_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            print(f"[pricing] variant {shopify_variant_id} permanently failed: {exc}", flush=True)
            return {"ok": False, "reason": "exception", "error": str(exc)}
        raise self.retry(exc=exc)
