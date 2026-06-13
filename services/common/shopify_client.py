"""
services/common/shopify_client.py

Tiny Shopify Admin GraphQL wrapper shared between sync (shopify_svc) and
write-back (pricing_svc). Offline access token comes from the Session table
that the React Router app writes during install.
"""
from __future__ import annotations

from sqlalchemy import text

from services.common.db import get_db

SHOPIFY_API_VERSION = "2026-07"


def get_offline_token(shop_domain: str) -> str | None:
    """Most-recent offline access token for this shop, or None."""
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


def shopify_graphql(shop_domain: str, token: str, query: str, variables: dict) -> dict:
    """One GraphQL POST against Shopify Admin. Raises on HTTP / GraphQL error."""
    import requests
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    resp = requests.post(
        url,
        json={"query": query, "variables": variables},
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
    return body.get("data", {})


VARIANT_BULK_UPDATE_MUTATION = """
mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id price }
    userErrors { field message }
  }
}
"""
