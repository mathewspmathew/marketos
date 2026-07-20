"""
services/shopify_svc/main.py

Shopify Admin API surface for the merchant product sync: pulls the
merchant's own products/variants via GraphQL and upserts them into
ShopifyProduct/ShopifyVariant.

Does NOT scrape competitors. Competitor scraping cadence is user-controlled
via ScrapingConfig.frequencyInterval/frequencyUnit; that path is unchanged.

Applying PriceDecisions to Shopify (write-back) lives in
services/pricing_svc/apply.py, not here — this file previously had its own
write-back path (apply_decision/sweep_pending) plus a SalesAggregate sync
cluster, both built against PricingRule/PricingConfig/SalesAggregate tables
that were dropped in the discovery_pivot migration
(shopify_ui/prisma/migrations/20260519040905_discovery_pivot). Removed here
since pricing_svc/apply.py is the current, schema-matching implementation.

The per-shop offline access token already lives in the existing Session
table — the Shopify React Router library writes one Session row per shop
install with isOnline=false (offline token, long-lived). We read from there
instead of duplicating into a new table.
"""
from __future__ import annotations

import json

import requests
import structlog
from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.scraper_svc.semantics import claim_and_enqueue_semantics

logger = structlog.get_logger(__name__)

SHOPIFY_API_VERSION = "2026-07"


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
# Single-product webhook update (products/update): manual-edit detection
# ─────────────────────────────────────────────────────────────────────────────
#
# Ported from shopify_ui/app/routes/webhooks.products.update.jsx. Uses
# dedicated SQL (not _UPSERT_PRODUCT_SQL/_UPSERT_VARIANT_SQL above) because
# this path needs inventoryQuantity, a semanticStatus/semanticVersion reset,
# and a semanticText reset that the bulk-pull SQL deliberately does not do.

def _map_webhook_product(payload: dict, shop_domain: str) -> dict:
    images = payload.get("images") or []
    image_url = (payload.get("image") or {}).get("src") or (images[0].get("src") if images else None)
    tags_str = payload.get("tags") or ""
    tags = [t.strip() for t in tags_str.split(",") if t.strip()]
    return {
        "id":          f"gid://shopify/Product/{payload['id']}",
        "shopDomain":  shop_domain,
        "title":       payload.get("title") or "",
        "description": payload.get("body_html") or "",
        "vendor":      payload.get("vendor"),
        "productType": payload.get("product_type") or "",
        "tags":        tags,
        "imageUrl":    image_url,
        "handle":      payload.get("handle"),
        "status":      (payload.get("status") or "active").upper(),
    }


def _map_webhook_variant(vpayload: dict, product_id: str) -> dict:
    options = {}
    if vpayload.get("option1"):
        options["Option1"] = vpayload["option1"]
    if vpayload.get("option2"):
        options["Option2"] = vpayload["option2"]
    if vpayload.get("option3"):
        options["Option3"] = vpayload["option3"]
    price = vpayload.get("price")
    return {
        "id":                f"gid://shopify/ProductVariant/{vpayload['id']}",
        "productId":         product_id,
        "title":             vpayload.get("title") or "Default Title",
        "currentPrice":      price if price is not None else "0",
        "compareAtPrice":    vpayload.get("compare_at_price"),
        "sku":               vpayload.get("sku"),
        "barcode":           vpayload.get("barcode"),
        "options":           options,
        "inventoryQuantity": vpayload.get("inventory_quantity"),
        # First-seen store price on a genuinely new variant = the anchor.
        # NULL when Shopify sends no price — never anchor at 0.
        "basePrice":         price if price is not None else None,
    }


_ENSURE_SHOPIFY_USER_SQL = text("""
INSERT INTO "ShopifyUser" ("shopDomain") VALUES (:sd)
ON CONFLICT ("shopDomain") DO NOTHING
""")

_UPSERT_PRODUCT_FROM_WEBHOOK_SQL = text("""
INSERT INTO "ShopifyProduct"
  (id, "shopDomain", title, description, vendor, "productType", tags, "imageUrl", handle, status,
   "semanticStatus", "semanticVersion", "updatedAt")
VALUES
  (:id, :shopDomain, :title, :description, :vendor, :productType, CAST(:tags AS jsonb), :imageUrl, :handle, :status,
   'PENDING', 1, NOW())
ON CONFLICT (id) DO UPDATE SET
  title             = EXCLUDED.title,
  description       = EXCLUDED.description,
  vendor            = EXCLUDED.vendor,
  "productType"     = EXCLUDED."productType",
  tags              = EXCLUDED.tags,
  "imageUrl"        = EXCLUDED."imageUrl",
  handle            = EXCLUDED.handle,
  status            = EXCLUDED.status,
  "semanticStatus"  = 'PENDING',
  "semanticVersion" = "ShopifyProduct"."semanticVersion" + 1,
  "updatedAt"       = NOW()
""")

# basePrice is deliberately NOT in the ON CONFLICT DO UPDATE SET list — an
# existing variant's basePrice is only ever changed by the explicit
# _SET_VARIANT_BASE_PRICE_SQL call below, and only when a manual edit was
# detected. Including it here unconditionally would re-anchor basePrice on
# every price change, including the pricing engine's own writes.
_UPSERT_VARIANT_FROM_WEBHOOK_SQL = text("""
INSERT INTO "ShopifyVariant"
  (id, "productId", title, "currentPrice", "compareAtPrice", sku, barcode, options,
   "inventoryQuantity", "semanticText", "basePrice", "updatedAt")
VALUES
  (:id, :productId, :title, :currentPrice, :compareAtPrice, :sku, :barcode, CAST(:options AS jsonb),
   :inventoryQuantity, NULL, :basePrice, NOW())
ON CONFLICT (id) DO UPDATE SET
  title               = EXCLUDED.title,
  "currentPrice"      = EXCLUDED."currentPrice",
  "compareAtPrice"    = EXCLUDED."compareAtPrice",
  sku                 = EXCLUDED.sku,
  barcode             = EXCLUDED.barcode,
  options             = EXCLUDED.options,
  "inventoryQuantity" = EXCLUDED."inventoryQuantity",
  "semanticText"      = NULL,
  "updatedAt"         = NOW()
""")

_SET_VARIANT_BASE_PRICE_SQL = text(
    'UPDATE "ShopifyVariant" SET "basePrice" = :base_price WHERE id = :vid'
)


@app.task(name="shopify_sync.handle_product_update")
def handle_product_update(shop_domain: str, payload: dict) -> dict:
    """Single-product webhook update: upsert + manual-edit detection.

    A price change is the pricing engine's own write-back iff it equals the
    latest applied PriceDecision.newPrice — anything else is a merchant
    manual edit, which re-anchors basePrice so the lifetime cap follows the
    merchant's new intent, and may recompute auto-derived min/max bounds.
    """
    shopify_id = f"gid://shopify/Product/{payload['id']}"
    try:
        with get_db() as session:
            session.execute(_ENSURE_SHOPIFY_USER_SQL, {"sd": shop_domain})

            p = _map_webhook_product(payload, shop_domain)
            p["tags"] = json.dumps(p["tags"])
            session.execute(_UPSERT_PRODUCT_FROM_WEBHOOK_SQL, p)

            prior_row = session.execute(
                text('SELECT "avgBasePrice", "minPriceOverride", "maxPriceOverride" '
                     'FROM "ShopifyProduct" WHERE id = :pid'),
                {"pid": shopify_id},
            ).first()

            any_manual_edit = False
            manual_edits: list[tuple[str, float, float]] = []

            for vpayload in (payload.get("variants") or []):
                variant_id = f"gid://shopify/ProductVariant/{vpayload['id']}"

                prior_price_row = session.execute(
                    text('SELECT "currentPrice" FROM "ShopifyVariant" WHERE id = :vid'),
                    {"vid": variant_id},
                ).first()
                prior_price = float(prior_price_row[0]) if prior_price_row else None
                raw_price = vpayload.get("price")
                new_price = float(raw_price) if raw_price is not None else None
                price_changed = (
                    prior_price is not None and new_price is not None
                    and abs(new_price - prior_price) > 0.005
                )

                is_manual_edit = False
                if price_changed:
                    latest = session.execute(
                        text('SELECT "newPrice" FROM "PriceDecision" '
                             'WHERE "shopifyVariantId" = :vid AND "appliedAt" IS NOT NULL '
                             'ORDER BY "decidedAt" DESC LIMIT 1'),
                        {"vid": variant_id},
                    ).first()
                    engine_wrote = latest is not None and abs(float(latest[0]) - new_price) <= 0.005
                    is_manual_edit = not engine_wrote

                v = _map_webhook_variant(vpayload, shopify_id)
                v["options"] = json.dumps(v["options"])
                session.execute(_UPSERT_VARIANT_FROM_WEBHOOK_SQL, v)

                if is_manual_edit and new_price is not None:
                    session.execute(_SET_VARIANT_BASE_PRICE_SQL, {"vid": variant_id, "base_price": new_price})
                    any_manual_edit = True
                    manual_edits.append((variant_id, prior_price, new_price))
                    logger.info(
                        "webhook_manual_price_edit_detected",
                        variant_id=variant_id, old_price=prior_price, new_price=new_price,
                    )

            avg_row = session.execute(
                text('SELECT AVG("basePrice") FROM "ShopifyVariant" WHERE "productId" = :pid'),
                {"pid": shopify_id},
            ).first()
            new_avg = float(avg_row[0]) if avg_row and avg_row[0] is not None else None

            session.execute(
                text('UPDATE "ShopifyProduct" SET "avgBasePrice" = :avg WHERE id = :pid'),
                {"avg": new_avg, "pid": shopify_id},
            )
            if any_manual_edit:
                session.execute(
                    text('UPDATE "ShopifyProduct" SET "lastDecisionAt" = NULL WHERE id = :pid'),
                    {"pid": shopify_id},
                )

                settings_row = session.execute(
                    text('SELECT "lifetimeCapPct" FROM "ShopSettings" WHERE "shopDomain" = :sd'),
                    {"sd": shop_domain},
                ).first()
                cap = float(settings_row[0]) if settings_row and settings_row[0] is not None else 0.25

                old_avg    = float(prior_row[0]) if prior_row and prior_row[0] is not None else None
                stored_min = float(prior_row[1]) if prior_row and prior_row[1] is not None else None
                stored_max = float(prior_row[2]) if prior_row and prior_row[2] is not None else None

                def _close(a, b):
                    return a is not None and b is not None and abs(a - b) <= 0.011

                if (old_avg is not None and new_avg is not None
                        and stored_min is not None and stored_max is not None
                        and _close(stored_min, old_avg * (1 - cap))
                        and _close(stored_max, old_avg * (1 + cap))):
                    session.execute(
                        text('UPDATE "ShopifyProduct" SET "minPriceOverride" = :min_p, '
                             '"maxPriceOverride" = :max_p WHERE id = :pid'),
                        {"min_p": round(new_avg * (1 - cap), 2), "max_p": round(new_avg * (1 + cap), 2), "pid": shopify_id},
                    )
                    logger.info("webhook_bounds_recomputed", shopify_id=shopify_id, new_avg=new_avg)

                for variant_id, old_price, new_price in manual_edits:
                    tracked_row = session.execute(
                        text(
                            'SELECT '
                            '(SELECT COUNT(*) FROM "ProductMatch" WHERE "shopifyVariantId" = :vid) + '
                            '(SELECT COUNT(*) FROM "PriceDecision" WHERE "shopifyVariantId" = :vid) AS cnt'
                        ),
                        {"vid": variant_id},
                    ).first()
                    tracked = bool(tracked_row and tracked_row[0] > 0)
                    if tracked and old_price is not None and new_price is not None:
                        session.execute(
                            text(
                                'INSERT INTO "PriceDecision" '
                                '(id, "shopDomain", "shopifyVariantId", "oldPrice", "newPrice", '
                                ' reason, "appliedAt", "autoApplied", "decidedAt") '
                                'VALUES (gen_random_uuid(), :sd, :vid, :op, :np, :reason, NOW(), FALSE, NOW())'
                            ),
                            {"sd": shop_domain, "vid": variant_id, "op": old_price, "np": new_price,
                             "reason": "manual price edit by merchant"},
                        )

            claim_and_enqueue_semantics(session, ids=[shopify_id])

        return {
            "ok": True,
            "manual_edit": any_manual_edit,
            "variants_updated": len(payload.get("variants") or []),
        }
    except Exception:
        logger.exception("handle_product_update_failed", shop_domain=shop_domain, shopify_id=shopify_id)
        raise
