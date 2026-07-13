"""Applies dynamic-pricing pane configuration extracted from a chat message,
directly — no ChatPreview card, no confirmation click. The one deliberate
exception to this chatbot's confirm-first convention (see
docs/superpowers/specs/2026-07-13-chatbot-apply-pane-config-design.md).
"""
from __future__ import annotations

from services.common.db import get_db
from services.common import models
from services.common.pane_config import (
    PaneConfig, PaneConfigError, apply_pane_config,
    pause_dynamic_pricing as _pause_dynamic_pricing,
)
from services.chatbot_svc.schemas import PaneConfigInput, ApplyPaneConfigResult, PauseDynamicPricingResult


def apply_dynamic_pricing_config(
    shop_domain: str, product_id: str, config: PaneConfigInput
) -> ApplyPaneConfigResult:
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        if product is None or product.shopDomain != shop_domain:
            raise RuntimeError(
                f"Product {product_id} not found in this shop. "
                f"Resolve it with resolve_product first."
            )

        pane_config = PaneConfig(
            search_query_override=config.search_query_override,
            pricing_tier=config.pricing_tier,
            min_price_override=config.min_price_override,
            max_price_override=config.max_price_override,
            frequency_unit=config.frequency_unit,
            frequency_interval=config.frequency_interval,
            discovery_num_results=config.discovery_num_results,
            listing_expansion_cap=config.listing_expansion_cap,
        )
        try:
            result = apply_pane_config(s, product, pane_config)
        except PaneConfigError as exc:
            raise RuntimeError(str(exc)) from exc

        before, after = result["dynamicPricingEnabled"]
        return ApplyPaneConfigResult(
            product_id=product.id,
            product_title=product.title,
            dynamic_pricing_enabled_before=before,
            dynamic_pricing_enabled_after=after,
            rearmed_count=result["rearmedCount"],
            human_summary=(
                f"Dynamic pricing is now on for {product.title}. "
                f"{result['rearmedCount']} scrape schedule(s) re-armed."
            ),
        )


def pause_dynamic_pricing(shop_domain: str, product_id: str) -> PauseDynamicPricingResult:
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        if product is None or product.shopDomain != shop_domain:
            raise RuntimeError(
                f"Product {product_id} not found in this shop. "
                f"Resolve it with resolve_product first."
            )

        result = _pause_dynamic_pricing(s, product)
        before = result["dynamicPricingEnabled"]["old"]
        after = result["dynamicPricingEnabled"]["new"]

        return PauseDynamicPricingResult(
            product_id=product.id,
            product_title=product.title,
            dynamic_pricing_enabled_before=before,
            dynamic_pricing_enabled_after=after,
            human_summary=f"Dynamic pricing is now paused for {product.title}.",
        )
