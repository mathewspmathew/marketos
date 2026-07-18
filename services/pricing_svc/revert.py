"""services/pricing_svc/revert.py

Reverts one variant's applied PriceDecision: pushes the old price back to
Shopify, marks the decision reverted, and pauses dynamic pricing for the
variant's PRODUCT (the only pause granularity that exists post the
product_level_pricing migration — ShopifyVariant.autoPriceEnabled no longer
exists). See docs/superpowers/specs/2026-07-19-price-revert-python-design.md.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from services.common import models
from services.common.pane_config import pause_dynamic_pricing
from services.common.shopify_auth import ShopifyAPIError, ShopifyAuthError, call_shopify_admin

_MUTATION = """
    mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
        productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id price }
            userErrors { field message }
        }
    }
"""


class RevertError(ValueError):
    """Raised when a revert request fails validation or Shopify rejects it."""


def revert_price_decision(
    session: Session, shop_domain: str, variant_id: str, decision_id: str,
) -> dict:
    variant = session.get(models.ShopifyVariant, variant_id)
    product = session.get(models.ShopifyProduct, variant.productId) if variant else None
    if variant is None or product is None or product.shopDomain != shop_domain:
        raise RevertError(f"Variant {variant_id} not found in this shop.")

    decision = session.get(models.PriceDecision, decision_id)
    if decision is None or decision.shopifyVariantId != variant_id:
        raise RevertError(f"Price change {decision_id} not found for this variant.")
    if decision.appliedAt is None:
        raise RevertError("This price change has not been applied yet, so it can't be reverted.")
    if decision.revertedAt is not None:
        raise RevertError("This price change has already been reverted.")

    target_price = Decimal(str(decision.oldPrice))

    try:
        result = call_shopify_admin(
            shop_domain,
            _MUTATION,
            {"productId": product.id, "variants": [{"id": variant_id, "price": f"{target_price:.2f}"}]},
            session,
        )
    except ShopifyAuthError as exc:
        raise RevertError(f"Could not authenticate with Shopify: {exc}") from exc
    except ShopifyAPIError as exc:
        raise RevertError(f"Shopify API error: {exc}") from exc

    user_errors = (result.get("data") or {}).get("productVariantsBulkUpdate", {}).get("userErrors") or []
    if user_errors:
        raise RevertError("; ".join(e["message"] for e in user_errors))

    old_price_before_revert = Decimal(str(variant.currentPrice))
    now = datetime.now(timezone.utc)

    decision.revertedAt = now
    variant.currentPrice = target_price
    pause_dynamic_pricing(session, product)

    session.add(models.PriceDecision(
        shopDomain=shop_domain,
        shopifyVariantId=variant_id,
        oldPrice=old_price_before_revert,
        newPrice=target_price,
        reason=f"manual_revert of change {decision_id}",
        appliedAt=now,
    ))

    return {"oldPrice": float(decision.oldPrice), "newPrice": float(decision.newPrice)}
