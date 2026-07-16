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
