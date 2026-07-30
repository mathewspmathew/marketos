"""
services/pricing_svc/apply.py

Apply a batch of PriceDecisions to Shopify via direct Token Exchange.

Token Exchange:
    Workers read the refresh token from the Session table, exchange it for
    an access token via Shopify's Token Exchange endpoint, then call the
    Admin API directly. No React Router proxy needed.

Idempotent: skips decisions that already have appliedAt set.
Per-product advisory lock prevents concurrent apply for the same product.
"""
from __future__ import annotations

import json
import os
import httpx
import structlog

from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.common.shopify_auth import (
    ShopifyAPIError,
    ShopifyAuthError,
    call_shopify_admin,
)

logger = structlog.get_logger(__name__)


def _stamp_error(session, decision_ids: list[str], err: str) -> None:
    if not decision_ids:
        return
    session.execute(
        text('UPDATE "PriceDecision" SET "applyError" = :e WHERE id = ANY(:ids)'),
        {"e": err[:500], "ids": decision_ids},
    )


def _notify_price_change(shop_domain: str, product_title: str, currency: str, variants: list[dict]) -> None:
    """Fire-and-forget POST to the JS app's internal notify-price-change route.

    Must never raise into the caller — a failed/timed-out notification should
    never block or fail the price-apply path that triggers it.
    """
    app_url = os.environ.get("APP_URL")
    token = os.environ.get("INTERNAL_API_TOKEN")
    if not app_url or not token:
        logger.warning("notify_price_change_skipped", reason="APP_URL/INTERNAL_API_TOKEN unset")
        return
    try:
        resp = httpx.post(
            f"{app_url}/internal/notify-price-change",
            headers={"X-Internal-Token": token},
            json={
                "shopDomain": shop_domain,
                "productTitle": product_title,
                "currency": currency,
                "variants": variants,
            },
            timeout=5.0,
        )
        if resp.status_code >= 300:
            logger.warning(
                "notify_price_change_non_2xx",
                status_code=resp.status_code,
                shop_domain=shop_domain,
                product_title=product_title,
            )
        else:
            logger.info(
                "notify_price_change_sent",
                status_code=resp.status_code,
                shop_domain=shop_domain,
                product_title=product_title,
            )
    except Exception:
        logger.warning("notify_price_change_failed", shop_domain=shop_domain, product_title=product_title)


def _apply(shop_domain: str, shopify_product_id: str, trigger_decision_id: str) -> dict:
    with get_db() as session:
        # Per-product advisory lock — held for the transaction. If a parallel
        # apply for this product is already in flight, skip cleanly instead
        # of racing on the pending lookup below.
        got_lock = session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"),
            {"k": f"apply:{shopify_product_id}"},
        ).scalar()
        if not got_lock:
            return {"ok": True, "noop": "apply_in_flight"}

        sibling_rows = session.execute(
            text("""
                SELECT DISTINCT ON (pd."shopifyVariantId")
                       pd.id, pd."shopifyVariantId", pd."oldPrice", pd."newPrice", pd."appliedAt",
                       v."title" AS "variantTitle"
                FROM "PriceDecision" pd
                JOIN "ShopifyVariant" v ON v.id = pd."shopifyVariantId"
                WHERE v."productId"   = :pid
                  AND pd."shopDomain" = :sd
                  AND pd."autoApplied" = TRUE
                ORDER BY pd."shopifyVariantId", pd."decidedAt" DESC
            """),
            {"pid": shopify_product_id, "sd": shop_domain},
        ).all()

        pending = [
            (r.id, r.shopifyVariantId, r.newPrice, r.oldPrice, r.variantTitle)
            for r in sibling_rows if r.appliedAt is None
        ]
        if not pending:
            return {"ok": True, "noop": "all_already_applied"}

        variants_input = [
            {"id": vid, "price": f"{float(price):.2f}"}
            for (_, vid, price, _old, _title) in pending
        ]

        # GraphQL mutation for updating variant prices
        mutation = """
            mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
                productVariantsBulkUpdate(productId: $productId, variants: $variants) {
                    productVariants { id price }
                    userErrors { field message }
                }
            }
        """

        # Call Shopify Admin API directly with Token Exchange
        try:
            result = call_shopify_admin(
                shop_domain,
                mutation,
                {"productId": shopify_product_id, "variants": variants_input},
                session,
            )
        except ShopifyAuthError as exc:
            msg = f"auth_error: {str(exc)}"
            _stamp_error(session, [d for d, _, _, _, _ in pending], msg)
            return {"ok": False, "reason": "unauthorized", "error": msg}
        except ShopifyAPIError as exc:
            msg = f"api_error: {str(exc)}"
            _stamp_error(session, [d for d, _, _, _, _ in pending], msg)
            return {"ok": False, "reason": "api_error", "error": msg}

        # Extract response data (GraphQL response format)
        bulk = (result.get("data") or {}).get("productVariantsBulkUpdate") or {}
        user_errors = bulk.get("userErrors") or []
        if user_errors:
            _stamp_error(
                session,
                [d for d, _, _, _, _ in pending],
                f"user_errors: {json.dumps(user_errors)[:400]}",
            )
            return {"ok": False, "reason": "user_errors", "userErrors": user_errors}

        decision_ids = [d for d, _, _, _, _ in pending]
        session.execute(
            text("""
                UPDATE "PriceDecision"
                   SET "appliedAt"       = NOW(),
                       "shopifyResponse" = CAST(:r AS jsonb)
                 WHERE id = ANY(:ids)
            """),
            {"ids": decision_ids, "r": json.dumps(bulk, default=str)},
        )
        for _did, vid, price, _old, _title in pending:
            session.execute(
                text('UPDATE "ShopifyVariant" SET "currentPrice" = :p WHERE id = :v'),
                {"p": float(price), "v": vid},
            )

        notify_row = session.execute(
            text("""
                SELECT sp."title" AS "productTitle", ss."currency",
                       ss."priceChangeNotificationsEnabled", ss."notifyEmail"
                FROM "ShopifyProduct" sp
                JOIN "ShopSettings" ss ON ss."shopDomain" = sp."shopDomain"
                WHERE sp.id = :pid
            """),
            {"pid": shopify_product_id},
        ).first()

    if notify_row and notify_row.priceChangeNotificationsEnabled and notify_row.notifyEmail:
        _notify_price_change(
            shop_domain,
            notify_row.productTitle,
            notify_row.currency,
            [
                {"variantTitle": title, "oldPrice": str(old), "newPrice": str(price)}
                for (_, _, price, old, title) in pending
            ],
        )

    return {"ok": True, "applied": len(pending), "decisionIds": decision_ids}


@app.task(name="pricing.apply_price", bind=True, max_retries=5, default_retry_delay=30)
def apply_price(self, shop_domain: str, shopify_product_id: str, trigger_decision_id: str):
    try:
        return _apply(shop_domain, shopify_product_id, trigger_decision_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.exception(
                "apply_price_permanently_failed",
                shopify_product_id=shopify_product_id,
                trigger_decision_id=trigger_decision_id,
            )
            return {"ok": False, "reason": "exception", "error": str(exc)}
        raise self.retry(exc=exc)
