"""
services/elasticity_svc/main.py

Celery tasks (elasticity_queue):
  elasticity.train_for_shop(shop_domain)
    Nightly job. Pulls historical (applied PriceDecision + stats + sales) rows, bootstraps
    with synthetic samples when sparse, trains three quantile regressors, and
    writes the artifacts to disk.

  elasticity.train_all_shops()
    Beat-fired wrapper that fans out train_for_shop tasks for every shop that
    has any pricing rows (or any variants — the bootstrap can train from
    synthetic data alone).

The inference path (suggest_price) is NOT a Celery task — it runs inline
inside pricing_svc.decide_price so the model contributes to every decision
in the same transaction, with model loading cached per-process.
"""
from __future__ import annotations

from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.elasticity_svc.training import train_for_shop as _train_for_shop


@app.task(name="elasticity.train_for_shop", bind=True, max_retries=2, default_retry_delay=120)
def train_for_shop(self, shop_domain: str) -> dict:
    try:
        with get_db() as session:
            result = _train_for_shop(session, shop_domain)
        print(f"[elasticity] {shop_domain}: {result}", flush=True)
        return result
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            print(f"[elasticity] {shop_domain} permanently failed: {exc}", flush=True)
            return {"ok": False, "reason": "exception", "error": str(exc)}
        raise self.retry(exc=exc)


@app.task(name="elasticity.train_all_shops")
def train_all_shops() -> int:
    """Find every shop with at least one variant and fan out training tasks."""
    with get_db() as session:
        shops = [
            r[0] for r in session.execute(
                text('SELECT DISTINCT "shopDomain" FROM "ShopifyProduct"'),
            ).all()
        ]
    for shop in shops:
        app.send_task(
            "elasticity.train_for_shop",
            args=[shop],
            queue="elasticity_queue",
        )
    print(f"[elasticity] dispatched training for {len(shops)} shop(s)", flush=True)
    return len(shops)
