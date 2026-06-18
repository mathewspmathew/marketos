"""
services/pricing_svc/apply.py

Apply a batch of PriceDecisions to Shopify via the React Router app's
internal write proxy (/internal/apply-price).

Why through the proxy and not directly?
    Shopify is migrating public apps to expiring offline access tokens.
    The library handles Token Exchange refresh on every authenticated call —
    but only when those calls go through the @shopify/shopify-app-react-router
    library (the React Router server). Background Python workers don't have
    access to that refresh machinery. So we treat the React Router app as
    the single source of Shopify auth: workers POST {productId, variants}
    over HTTP with a shared secret; the library does the rest.

Idempotent: skips decisions that already have appliedAt set.
Per-product advisory lock prevents concurrent apply for the same product.
"""
from __future__ import annotations

import json
import logging
import os

import requests
from sqlalchemy import text

from services.common.activity_logger import log_decision_applied, log_decision_failed
from services.common.celery_app import app
from services.common.db import get_db

logger = logging.getLogger(__name__)


def _stamp_error(session, decision_ids: list[str], err: str, shop_domain: str = "", pending: list[tuple] = None) -> None:
    if not decision_ids:
        return
    session.execute(
        text('UPDATE "PriceDecision" SET "applyError" = :e WHERE id = ANY(:ids)'),
        {"e": err[:500], "ids": decision_ids},
    )
    # Log failures for activity tracking
    if pending and shop_domain:
        for did, vid, _ in pending:
            if did in decision_ids:
                try:
                    # Try to fetch the product ID from the variant
                    product_result = session.execute(
                        text('SELECT "productId" FROM "ShopifyVariant" WHERE id = :v'),
                        {"v": vid},
                    ).scalar()
                    product_id = product_result if product_result else ""
                    log_decision_failed(
                        shop_domain=shop_domain,
                        price_decision_id=did,
                        shopify_product_id=product_id,
                        shopify_variant_id=vid,
                        error_message=err[:500],
                    )
                except Exception as e:
                    logger.exception(f"Failed to log decision failure: {e}")


def _post_to_proxy(shop_domain: str, product_id: str, variants_input: list[dict]) -> dict:
    base_url = os.environ.get("REACT_ROUTER_INTERNAL_URL")
    token    = os.environ.get("INTERNAL_API_TOKEN")
    if not base_url or not token:
        raise RuntimeError("REACT_ROUTER_INTERNAL_URL / INTERNAL_API_TOKEN not configured")
    resp = requests.post(
        f"{base_url.rstrip('/')}/internal/apply-price",
        json={"shopDomain": shop_domain, "productId": product_id, "variants": variants_input},
        headers={"X-Internal-Token": token, "Content-Type": "application/json"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"proxy {resp.status_code}: {resp.text[:300]}")
    return resp.json()


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

        variants_input = [
            {"id": vid, "price": f"{float(price):.2f}"}
            for (_, vid, price) in pending
        ]

        try:
            proxy_result = _post_to_proxy(shop_domain, shopify_product_id, variants_input)
        except Exception as exc:
            msg = str(exc)
            _stamp_error(session, [d for d, _, _ in pending], f"proxy_failed: {msg}", shop_domain, pending)
            # 401 from the proxy → no Session row exists; pointless to retry
            # until the merchant reopens the app.
            if "401" in msg or "no_session_for_shop" in msg:
                return {"ok": False, "reason": "unauthorized"}
            raise

        if not proxy_result.get("ok"):
            err = proxy_result.get("error") or proxy_result.get("reason") or "unknown"
            _stamp_error(session, [d for d, _, _ in pending], f"proxy_returned_not_ok: {err}"[:500], shop_domain, pending)
            return {"ok": False, "reason": err}

        # The proxy returns { ok: true, data: { productVariantsBulkUpdate: {...} } }
        bulk = (proxy_result.get("data") or {}).get("productVariantsBulkUpdate") or {}
        user_errors = bulk.get("userErrors") or []
        if user_errors:
            _stamp_error(
                session,
                [d for d, _, _ in pending],
                f"user_errors: {json.dumps(user_errors)[:400]}",
                shop_domain,
                pending,
            )
            return {"ok": False, "reason": "user_errors", "userErrors": user_errors}

        decision_ids = [d for d, _, _ in pending]
        session.execute(
            text("""
                UPDATE "PriceDecision"
                   SET "appliedAt"       = NOW(),
                       "shopifyResponse" = CAST(:r AS jsonb)
                 WHERE id = ANY(:ids)
            """),
            {"ids": decision_ids, "r": json.dumps(bulk, default=str)},
        )
        for _did, vid, price in pending:
            session.execute(
                text('UPDATE "ShopifyVariant" SET "currentPrice" = :p WHERE id = :v'),
                {"p": float(price), "v": vid},
            )

        # Log successful applies for activity tracking
        for did, vid, _ in pending:
            try:
                # Try to fetch the product ID from the variant
                product_result = session.execute(
                    text('SELECT "productId" FROM "ShopifyVariant" WHERE id = :v'),
                    {"v": vid},
                ).scalar()
                product_id = product_result if product_result else ""
                log_decision_applied(
                    shop_domain=shop_domain,
                    price_decision_id=did,
                    shopify_product_id=product_id,
                    shopify_variant_id=vid,
                )
            except Exception as e:
                logger.exception(f"Failed to log decision applied: {e}")

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
