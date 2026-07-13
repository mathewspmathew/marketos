"""
services/shopify_svc/main.py

Single Shopify Admin API surface — both reading (sync_back: merchant orders,
inventory) and writing (write_back: applying PriceDecisions to variants).
Both directions share the same offline access token lookup and GraphQL
client wrapper, so they live in one package.

Sync path keeps SalesAggregate and ShopifyVariant.inventoryQuantity current:

  - Live deltas: webhooks in shopify_ui/app/routes/webhooks.* increment
    SalesAggregate counters and update inventoryQuantity on every order
    or product change.

  - Daily rebuild (this file): once a day, recompute the rolling 7d/28d
    window from Shopify Admin GraphQL. Webhook-only increments would drift
    upward forever — orders that should age out of the window need an
    explicit recompute. This also recomputes daysOfStock.

Does NOT scrape competitors. Competitor scraping cadence is user-controlled
via ScrapingConfig.frequencyInterval/frequencyUnit; that path is unchanged.

The per-shop offline access token already lives in the existing Session
table — the Shopify React Router library writes one Session row per shop
install with isOnline=false (offline token, long-lived). We read from there
instead of duplicating into a new table.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
import structlog
from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.scraper_svc.semantics import claim_and_enqueue_semantics

logger = structlog.get_logger(__name__)

SHOPIFY_API_VERSION = "2026-07"
_DEFAULT_LOOKBACK_DAYS = 28


# ─────────────────────────────────────────────────────────────────────────────
# Token + API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_offline_token(shop_domain: str) -> str | None:
    """Return the per-shop offline access token from the Shopify Session table.

    The Shopify React Router library stores one offline (isOnline=false)
    Session row per shop on install. Pick the most-recently-updated one
    in case Shopify has rotated tokens (expiringOfflineAccessTokens=true).
    """
    with get_db() as session:
        row = session.execute(
            text(
                'SELECT "accessToken" FROM "Session" '
                'WHERE shop = :sd AND "isOnline" = FALSE '
                'ORDER BY COALESCE(expires, NOW() + INTERVAL \'100 years\') DESC '
                'LIMIT 1'
            ),
            {"sd": shop_domain},
        ).first()
    return row[0] if row else None


def _shopify_graphql(shop_domain: str, token: str, query: str, variables: dict) -> dict:
    """Single GraphQL POST to Shopify Admin API. Caller handles retries.

    Returns the parsed `data` object on success; raises on HTTP / GraphQL error.
    """
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json={"query": query, "variables": variables}, headers=headers, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
    return body.get("data", {})


# Rolling-window aggregation: sum line_items by variant for orders in
# [now - lookback_days, now]. We use orders.createdAt and line item
# `originalUnitPriceSet.shopMoney.amount × quantity` for revenue.
_ORDERS_QUERY = """
query Orders($cursor: String, $query: String!) {
  orders(first: 100, after: $cursor, query: $query, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      createdAt
      lineItems(first: 50) {
        nodes {
          quantity
          variant { id }
          originalUnitPriceSet { shopMoney { amount } }
        }
      }
    }
  }
}
"""


def _fetch_orders(shop_domain: str, token: str, days: int) -> list[dict]:
    """Fetch all orders in the last `days` days, paginated. Returns flat
    list of {variantId, quantity, revenue, createdAt}."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    line_rows: list[dict] = []
    cursor: str | None = None

    while True:
        data = _shopify_graphql(
            shop_domain, token, _ORDERS_QUERY,
            {"cursor": cursor, "query": f"created_at:>={since}"},
        )
        conn = data.get("orders", {})
        for order in conn.get("nodes", []):
            created_at = order.get("createdAt")
            for li in order.get("lineItems", {}).get("nodes", []):
                variant = li.get("variant") or {}
                vid = variant.get("id")
                if not vid:
                    continue
                qty = int(li.get("quantity") or 0)
                unit = Decimal(li.get("originalUnitPriceSet", {}).get("shopMoney", {}).get("amount") or "0")
                line_rows.append({
                    "variantId":  vid,
                    "quantity":   qty,
                    "revenue":    qty * unit,
                    "createdAt":  created_at,
                })
        page = conn.get("pageInfo", {})
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")

    return line_rows


# ─────────────────────────────────────────────────────────────────────────────
# Product pull: GraphQL → model field mappers (pure)
# ─────────────────────────────────────────────────────────────────────────────

def _map_product_node(node: dict, shop_domain: str) -> dict:
    """Shopify product GraphQL node → ShopifyProduct synced-column dict.
    Only columns the sync owns; merchant-config columns are never touched."""
    return {
        "id":          node["id"],
        "shopDomain":  shop_domain,
        "title":       node.get("title") or "",
        "description": node.get("descriptionHtml") or "",
        "vendor":      node.get("vendor"),
        "productType": node.get("productType") or "",
        "tags":        node.get("tags") or [],
        "imageUrl":    (node.get("featuredImage") or {}).get("url"),
        "handle":      node.get("handle"),
        "status":      node.get("status") or "ACTIVE",
    }


def _map_variant_node(vnode: dict, product_id: str) -> dict:
    """Shopify variant GraphQL node → ShopifyVariant synced-column dict."""
    options = {
        opt["name"]: opt["value"]
        for opt in (vnode.get("selectedOptions") or [])
    }
    return {
        "id":             vnode["id"],
        "productId":      product_id,
        "title":          vnode.get("title") or "Default Title",
        "currentPrice":   vnode.get("price") or "0",
        "compareAtPrice": vnode.get("compareAtPrice"),
        "sku":            vnode.get("sku"),
        "barcode":        vnode.get("barcode"),
        "imageUrl":       (vnode.get("image") or {}).get("url"),
        "options":        options,
        # First-seen store price = the anchor dynamic pricing measures drift
        # from. NULL when Shopify sends no price — never anchor at 0.
        "basePrice":      vnode.get("price") or None,
    }


def _diff_new_ids(existing_ids: set[str], pulled_ids: list[str]) -> list[str]:
    """Product ids present in the pull but not already in the DB."""
    return [pid for pid in pulled_ids if pid not in existing_ids]


# ─────────────────────────────────────────────────────────────────────────────
# Product pull: GraphQL fetch + upsert task
# ─────────────────────────────────────────────────────────────────────────────

_PRODUCTS_QUERY = """
query Products($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id title descriptionHtml vendor productType handle status tags
        featuredImage { url }
        variants(first: 100) {
          edges {
            node {
              id title price compareAtPrice sku barcode
              image { url }
              selectedOptions { name value }
            }
          }
        }
      }
    }
  }
}
"""


def _fetch_products(shop_domain: str, token: str) -> list[dict]:
    """All products for the shop, paginated. Returns the raw GraphQL nodes."""
    nodes: list[dict] = []
    cursor: str | None = None
    while True:
        data = _shopify_graphql(shop_domain, token, _PRODUCTS_QUERY, {"cursor": cursor})
        conn = data.get("products", {})
        nodes.extend(edge["node"] for edge in conn.get("edges", []))
        page = conn.get("pageInfo", {})
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return nodes


_UPSERT_PRODUCT_SQL = text("""
INSERT INTO "ShopifyProduct"
  (id, "shopDomain", title, description, vendor, "productType", tags, "imageUrl", handle, status, "updatedAt")
VALUES
  (:id, :shopDomain, :title, :description, :vendor, :productType, CAST(:tags AS jsonb), :imageUrl, :handle, :status, NOW())
ON CONFLICT (id) DO UPDATE SET
  title         = EXCLUDED.title,
  description   = EXCLUDED.description,
  vendor        = EXCLUDED.vendor,
  "productType" = EXCLUDED."productType",
  tags          = EXCLUDED.tags,
  "imageUrl"    = EXCLUDED."imageUrl",
  handle        = EXCLUDED.handle,
  status        = EXCLUDED.status,
  "updatedAt"   = NOW()
""")

_UPSERT_VARIANT_SQL = text("""
INSERT INTO "ShopifyVariant"
  (id, "productId", title, "currentPrice", "compareAtPrice", sku, barcode, "imageUrl", options, "basePrice", "updatedAt")
VALUES
  (:id, :productId, :title, :currentPrice, :compareAtPrice, :sku, :barcode, :imageUrl, CAST(:options AS jsonb), :basePrice, NOW())
ON CONFLICT (id) DO UPDATE SET
  title            = EXCLUDED.title,
  "currentPrice"   = EXCLUDED."currentPrice",
  "compareAtPrice" = EXCLUDED."compareAtPrice",
  sku              = EXCLUDED.sku,
  barcode          = EXCLUDED.barcode,
  "imageUrl"       = EXCLUDED."imageUrl",
  options          = EXCLUDED.options,
  "basePrice"      = COALESCE("ShopifyVariant"."basePrice", EXCLUDED."basePrice"),
  "updatedAt"      = NOW()
""")

# avgBasePrice is a derived display value: average of the product's variant
# anchors. Recomputed after every variant upsert batch so it tracks variant
# adds/removals and basePrice fills.
_RECOMPUTE_AVG_BASE_SQL = text("""
UPDATE "ShopifyProduct" SET "avgBasePrice" = (
  SELECT AVG("basePrice") FROM "ShopifyVariant" WHERE "productId" = :pid
) WHERE id = :pid
""")


def _set_sync_state(session, shop_domain: str, state: str,
                    error: str | None = None, synced: bool = False) -> None:
    session.execute(
        text("""
            UPDATE "ShopifyUser" SET
              "productSyncState" = :state,
              "productSyncError" = :error,
              "productSyncedAt"  = CASE WHEN :synced THEN NOW() ELSE "productSyncedAt" END
            WHERE "shopDomain" = :sd
        """),
        {"state": state, "error": error, "synced": synced, "sd": shop_domain},
    )


@app.task(name="shopify_sync.pull_products")
def pull_products(shop_domain: str) -> dict:
    """Durable full product pull: Shopify Admin GraphQL → Postgres upsert.
    Updates ShopifyUser.productSyncState so the UI can show a passive indicator.
    Note: only NEW products trigger semantics; variant updates preserve
    semanticText to avoid re-embedding the whole catalogue on every refresh."""
    token = _get_offline_token(shop_domain)
    if not token:
        with get_db() as session:
            _set_sync_state(session, shop_domain, "ERROR", error="no_offline_token")
        return {"ok": False, "reason": "no_offline_token"}

    try:
        nodes = _fetch_products(shop_domain, token)
        with get_db() as session:
            existing = {
                row[0] for row in session.execute(
                    text('SELECT id FROM "ShopifyProduct" WHERE "shopDomain" = :sd'),
                    {"sd": shop_domain},
                )
            }
            pulled_ids = [n["id"] for n in nodes]
            new_ids = _diff_new_ids(existing, pulled_ids)

            for node in nodes:
                p = _map_product_node(node, shop_domain)
                p["tags"] = json.dumps(p["tags"])
                session.execute(_UPSERT_PRODUCT_SQL, p)
                for vedge in node.get("variants", {}).get("edges", []):
                    v = _map_variant_node(vedge["node"], node["id"])
                    v["options"] = json.dumps(v["options"])
                    session.execute(_UPSERT_VARIANT_SQL, v)
                session.execute(_RECOMPUTE_AVG_BASE_SQL, {"pid": node["id"]})

            _set_sync_state(session, shop_domain, "SYNCED", synced=True)

        if new_ids:
            with get_db() as session:
                claim_and_enqueue_semantics(session, ids=new_ids)
        return {"ok": True, "count": len(nodes), "new": len(new_ids)}

    except Exception as exc:  # noqa: BLE001 — record then re-raise for Celery retry
        with get_db() as session:
            _set_sync_state(session, shop_domain, "ERROR", error=str(exc)[:500])
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation + write-back
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_windows(rows: list[dict], now: datetime) -> dict[str, dict]:
    """Bucket line items into 7d / 28d windows, grouped by variant."""
    cutoff_7d  = now - timedelta(days=7)
    cutoff_28d = now - timedelta(days=28)
    out: dict[str, dict] = {}
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if ts < cutoff_28d:
            continue
        v = out.setdefault(r["variantId"], {
            "orders7d": 0, "revenue7d": Decimal("0"),
            "orders28d": 0, "revenue28d": Decimal("0"),
        })
        v["orders28d"]  += r["quantity"]
        v["revenue28d"] += r["revenue"]
        if ts >= cutoff_7d:
            v["orders7d"]  += r["quantity"]
            v["revenue7d"] += r["revenue"]
    return out


def _write_sales_aggregates(shop_domain: str, agg: dict[str, dict]) -> int:
    """Upsert SalesAggregate rows; also compute daysOfStock when inventory > 0."""
    if not agg:
        return 0
    written = 0
    with get_db() as session:
        for variant_id, w in agg.items():
            # daysOfStock = inventoryQuantity ÷ (orders28d / 28); guarded against
            # zero velocity (None means "unknown / infinite supply" downstream).
            inv_row = session.execute(
                text('SELECT "inventoryQuantity" FROM "ShopifyVariant" WHERE id = :v'),
                {"v": variant_id},
            ).first()
            inv = inv_row[0] if inv_row and inv_row[0] is not None else None
            velocity_per_day = w["orders28d"] / 28.0 if w["orders28d"] else 0
            days_of_stock = (inv / velocity_per_day) if (inv is not None and velocity_per_day > 0) else None

            session.execute(
                text(
                    'INSERT INTO "SalesAggregate" '
                    '("shopifyVariantId", "shopDomain", "orders7d", "revenue7d", '
                    ' "orders28d", "revenue28d", "daysOfStock", "updatedAt") '
                    'VALUES (:v, :s, :o7, :r7, :o28, :r28, :dos, NOW()) '
                    'ON CONFLICT ("shopifyVariantId") DO UPDATE SET '
                    '  "orders7d"    = EXCLUDED."orders7d", '
                    '  "revenue7d"   = EXCLUDED."revenue7d", '
                    '  "orders28d"   = EXCLUDED."orders28d", '
                    '  "revenue28d"  = EXCLUDED."revenue28d", '
                    '  "daysOfStock" = EXCLUDED."daysOfStock", '
                    '  "updatedAt"   = NOW()'
                ),
                {
                    "v": variant_id, "s": shop_domain,
                    "o7": w["orders7d"], "r7": w["revenue7d"],
                    "o28": w["orders28d"], "r28": w["revenue28d"],
                    "dos": float(days_of_stock) if days_of_stock is not None else None,
                },
            )
            written += 1
    return written


def refresh_for_shop(shop_domain: str) -> int:
    """Recompute SalesAggregate for one shop. Returns rows written."""
    token = _get_offline_token(shop_domain)
    if not token:
        logger.warning("no_offline_token", shop_domain=shop_domain)
        return 0
    rows = _fetch_orders(shop_domain, token, _DEFAULT_LOOKBACK_DAYS)
    agg = _aggregate_windows(rows, datetime.now(timezone.utc))
    return _write_sales_aggregates(shop_domain, agg)


# ─────────────────────────────────────────────────────────────────────────────
# Celery tasks
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name="shopify_sync.recompute_sales_aggregate", bind=True, max_retries=3, default_retry_delay=60)
def recompute_sales_aggregate(self, shop_domain: str):
    """Recompute SalesAggregate for one shop on demand."""
    try:
        return refresh_for_shop(shop_domain)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.exception("sales_aggregate_recompute_permanently_failed", shop_domain=shop_domain)
            return 0
        raise self.retry(exc=exc)


@app.task(name="shopify_sync.refresh_all_sales_aggregates", bind=True)
def refresh_all_sales_aggregates(self):
    """Daily fan-out: queue a recompute task per installed shop."""
    with get_db() as session:
        shops = [
            r[0] for r in session.execute(
                text('SELECT "shopDomain" FROM "ShopifyUser"')
            ).all()
        ]
    for shop in shops:
        app.send_task(
            "shopify_sync.recompute_sales_aggregate",
            args=[shop],
            queue="shopify_sync_queue",
        )
    logger.info("daily_sales_refresh_queued", shop_count=len(shops))
    return len(shops)


# ═════════════════════════════════════════════════════════════════════════════
# WRITE-BACK — apply PriceDecisions to Shopify
# ═════════════════════════════════════════════════════════════════════════════

# productVariantUpdate has been deprecated for bulk; we use
# productVariantsBulkUpdate which accepts up to 250 variants in one call.
# For v1 we apply one variant at a time per task (simple, retryable). The
# bulk path is a follow-up once volumes demand it.
_VARIANT_UPDATE_MUTATION = """
mutation productVariantUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id price }
    userErrors { field message }
  }
}
"""


def _flag_decision(session, decision_id: str, reason: str, severity: str, payload: dict | None = None) -> None:
    """Mark a PriceDecision as alert-worthy. Folded from the old PricingAlert
    table: alertReason != NULL means it surfaces in the alerts feed; payload
    is stashed under shopifyResponse so the UI can render details."""
    import json
    session.execute(
        text(
            'UPDATE "PriceDecision" SET '
            '  "alertReason"     = :r, '
            '  "alertSeverity"   = :sv, '
            '  "shopifyResponse" = COALESCE("shopifyResponse", CAST(:p AS jsonb)) '
            'WHERE id = :i'
        ),
        {"i": decision_id, "r": reason, "sv": severity,
         "p": json.dumps(payload or {}, default=str)},
    )


def _apply_one_decision(decision_id: str) -> dict:
    """Pull the decision, validate it's still applicable, push to Shopify,
    flip PriceDecision.appliedAt + persist shopifyResponse.

    Idempotent: if the variant is already at the decision's newPrice we
    short-circuit. Safe to retry."""
    import json
    with get_db() as session:
        d = session.execute(
            text("""
                SELECT pd.id, pd."shopDomain", pd."shopifyVariantId",
                       pd."oldPrice", pd."newPrice", pd."blockedBy",
                       pd."appliedAt", pd."ruleId",
                       pr."autoApply", pr.enabled AS rule_enabled,
                       sv."productId", sv."currentPrice",
                       pc."killSwitch"
                FROM "PriceDecision" pd
                LEFT JOIN "PricingRule"   pr ON pr.id = pd."ruleId"
                LEFT JOIN "ShopifyVariant" sv ON sv.id = pd."shopifyVariantId"
                LEFT JOIN "PricingConfig" pc ON pc."shopDomain" = pd."shopDomain"
                WHERE pd.id = :d
            """),
            {"d": decision_id},
        ).first()
        if not d:
            return {"ok": False, "reason": "decision_not_found"}
        if d.appliedAt is not None:
            return {"ok": True, "noop": "already_applied"}
        if d.blockedBy is not None:
            return {"ok": False, "reason": f"blocked:{d.blockedBy}"}
        if d.autoApply is not True:
            return {"ok": False, "reason": "rule_not_auto_apply"}
        if d.rule_enabled is False:
            return {"ok": False, "reason": "rule_disabled"}
        if d.killSwitch is True:
            return {"ok": False, "reason": "kill_switch_on"}

        shop_domain = d.shopDomain
        new_price = float(d.newPrice)
        current   = float(d.currentPrice) if d.currentPrice is not None else None

        if current is not None and abs(new_price - current) < 0.005:
            session.execute(
                text('UPDATE "PriceDecision" SET "appliedAt" = NOW() WHERE id = :i'),
                {"i": decision_id},
            )
            return {"ok": True, "noop": "price_already_matches"}

        token = _get_offline_token(shop_domain)
        if not token:
            _flag_decision(session, decision_id, "MISSING_OFFLINE_TOKEN", "CRITICAL",
                           {"hint": "merchant must reopen the app to refresh OAuth"})
            return {"ok": False, "reason": "no_offline_token"}

        try:
            data = _shopify_graphql(
                shop_domain, token, _VARIANT_UPDATE_MUTATION,
                {
                    "productId": d.productId,
                    "variants": [{"id": d.shopifyVariantId, "price": f"{new_price:.2f}"}],
                },
            )
        except Exception as exc:
            _flag_decision(session, decision_id, "SHOPIFY_WRITE_FAILED", "WARN",
                           {"error": str(exc)})
            raise  # let Celery retry

        result = data.get("productVariantsBulkUpdate", {})
        user_errors = result.get("userErrors") or []
        if user_errors:
            _flag_decision(session, decision_id, "SHOPIFY_USER_ERROR", "WARN",
                           {"userErrors": user_errors})
            return {"ok": False, "reason": "user_errors", "userErrors": user_errors}

        # Flip applied + persist Shopify response + update local price snapshot.
        session.execute(
            text(
                'UPDATE "PriceDecision" SET '
                '  "appliedAt"       = NOW(), '
                '  "shopifyResponse" = CAST(:r AS jsonb) '
                'WHERE id = :i'
            ),
            {"i": decision_id, "r": json.dumps(result, default=str)},
        )
        session.execute(
            text('UPDATE "ShopifyVariant" SET "currentPrice" = :p WHERE id = :v'),
            {"p": round(new_price, 2), "v": d.shopifyVariantId},
        )

    return {"ok": True, "applied": True, "oldPrice": current, "newPrice": new_price}


@app.task(name="shopify_writer.apply_decision", bind=True,
          max_retries=5, default_retry_delay=30)
def apply_decision(self, decision_id: str):
    """Write one PriceDecision to Shopify. Retries on 429/5xx; non-retryable
    business errors (rule disabled, kill switch, etc.) return cleanly."""
    try:
        return _apply_one_decision(decision_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            logger.exception("apply_decision_permanently_failed", decision_id=decision_id)
            return {"ok": False, "reason": "exception", "error": str(exc)}
        raise self.retry(exc=exc)


@app.task(name="shopify_writer.sweep_pending", bind=True)
def sweep_pending(self):
    """Beat-driven safety net: pick up any unapplied auto-apply decisions
    that missed their immediate enqueue from pricing_svc (worker crash,
    redis blip, etc.). Bounded to last hour to avoid replaying ancient
    intent that the merchant may have since reverted via the UI."""
    with get_db() as session:
        rows = session.execute(
            text("""
                SELECT pd.id
                FROM "PriceDecision" pd
                JOIN "PricingRule" pr ON pr.id = pd."ruleId"
                LEFT JOIN "PricingConfig" pc ON pc."shopDomain" = pd."shopDomain"
                WHERE pd."appliedAt" IS NULL
                  AND pd."blockedBy" IS NULL
                  AND pr."autoApply" = TRUE
                  AND pr.enabled = TRUE
                  AND COALESCE(pc."killSwitch", FALSE) = FALSE
                  AND pd."decidedAt" > NOW() - INTERVAL '1 hour'
                ORDER BY pd."decidedAt" ASC
                LIMIT 500
            """),
        ).all()
    for (did,) in rows:
        app.send_task("shopify_writer.apply_decision", args=[did], queue="writer_queue")
    if rows:
        logger.info("pending_decisions_swept", count=len(rows))
    return len(rows)
