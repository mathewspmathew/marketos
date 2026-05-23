"""
services/pricing_svc/apply.py

Apply a batch of PriceDecisions (one per variant of a single product) to
Shopify in a single productVariantsBulkUpdate call. Triggered from
decide.decide_price_for_product when at least one variant decision was
written with autoApplied=true.

Idempotent: skips decisions that already have appliedAt set, or whose
variant is already at newPrice.

No human approval — by design. The decide step has already enforced every
gate (eligibility, caps, kill switch). This task only knows how to push.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.common.shopify_client import (
    VARIANT_BULK_UPDATE_MUTATION,
    get_offline_token,
    shopify_graphql,
)

logger = logging.getLogger(__name__)


def _stamp_error(session, decision_ids: list[str], err: str) -> None:
    if not decision_ids:
        return
    session.execute(
        text('UPDATE "PriceDecision" SET "applyError" = :e WHERE id = ANY(:ids)'),
        {"e": err[:500], "ids": decision_ids},
    )


def _apply(shop_domain: str, shopify_product_id: str, trigger_decision_id: str) -> dict:
    with get_db() as session:
        # Per-product advisory lock — held for the transaction. If a parallel
        # apply for this product is already in flight, skip cleanly instead
        # of racing on the "pending decisions" lookup below and risking
        # a double-push to Shopify.
        got_lock = session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
            {"k": f"apply:{shopify_product_id}"},
        ).scalar()
        if not got_lock:
            return {"ok": True, "noop": "apply_in_flight"}

        # Find sibling decisions written in the same batch as the trigger.
        # All variants of this product whose latest decision is autoApplied
        # and not yet pushed are considered part of the batch.
        sibling_rows = session.execute(
            text("""
                SELECT DISTINCT ON (pd."shopifyVariantId")
                       pd.id, pd."shopifyVariantId", pd."newPrice", pd."appliedAt"
                FROM "PriceDecision" pd
                JOIN "ShopifyVariant" v ON v.id = pd."shopifyVariantId"
                WHERE v."productId"   = :pid
                  AND pd."shopDomain" = :sd
                  AND pd."autoApplied" = TRUE
                ORDER BY pd."shopifyVariantId", pd."decidedAt" DESC
            """),
            {"pid": shopify_product_id, "sd": shop_domain},
        ).all()

        pending = [(r.id, r.shopifyVariantId, r.newPrice) for r in sibling_rows if r.appliedAt is None]
        if not pending:
            return {"ok": True, "noop": "all_already_applied"}

        token = get_offline_token(shop_domain)
        if not token:
            _stamp_error(session, [d for d, _, _ in pending], "missing_offline_token")
            return {"ok": False, "reason": "no_offline_token"}

        variants_input = [
            {"id": vid, "price": f"{float(price):.2f}"}
            for (_, vid, price) in pending
        ]

        try:
            data = shopify_graphql(
                shop_domain, token, VARIANT_BULK_UPDATE_MUTATION,
                {"productId": shopify_product_id, "variants": variants_input},
            )
        except Exception as exc:
            msg = str(exc)
            _stamp_error(session, [d for d, _, _ in pending], f"graphql_failed: {msg}")
            # 401 means the token is gone — retrying just burns cycles until
            # the merchant reopens the app. Bail cleanly so the worker stops
            # hammering Shopify; the token-expiry banner on app._index is the
            # signal to fix this.
            if "401" in msg or "Unauthorized" in msg:
                return {"ok": False, "reason": "unauthorized"}
            raise

        result = data.get("productVariantsBulkUpdate", {}) or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            _stamp_error(
                session,
                [d for d, _, _ in pending],
                f"user_errors: {json.dumps(user_errors)[:400]}",
            )
            return {"ok": False, "reason": "user_errors", "userErrors": user_errors}

        # Stamp appliedAt + persist response on every sibling decision; sync
        # ShopifyVariant.currentPrice locally so the next decide cycle reads truth.
        decision_ids = [d for d, _, _ in pending]
        session.execute(
            text("""
                UPDATE "PriceDecision"
                   SET "appliedAt"       = NOW(),
                       "shopifyResponse" = CAST(:r AS jsonb)
                 WHERE id = ANY(:ids)
            """),
            {"ids": decision_ids, "r": json.dumps(result, default=str)},
        )
        for _did, vid, price in pending:
            session.execute(
                text('UPDATE "ShopifyVariant" SET "currentPrice" = :p WHERE id = :v'),
                {"p": float(price), "v": vid},
            )

    return {"ok": True, "applied": len(pending), "decisionIds": decision_ids}


@app.task(name="pricing.apply_price", bind=True, max_retries=5, default_retry_delay=30)
def apply_price(self, shop_domain: str, shopify_product_id: str, trigger_decision_id: str):
    try:
        return _apply(shop_domain, shopify_product_id, trigger_decision_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.error(
                "pricing.apply_price product=%s decision=%s permanently failed: %s",
                shopify_product_id, trigger_decision_id, exc,
            )
            return {"ok": False, "reason": "exception", "error": str(exc)}
        raise self.retry(exc=exc)
