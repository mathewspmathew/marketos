"""Applies dynamic-pricing pane configuration extracted from a chat message,
directly — no ChatPreview card, no confirmation click. The one deliberate
exception to this chatbot's confirm-first convention (see
docs/superpowers/specs/2026-07-13-chatbot-apply-pane-config-design.md).
"""
from __future__ import annotations

import structlog

from services.common.db import get_db
from services.common import models
from services.common.pane_config import (
    PaneConfig, PaneConfigError, apply_pane_config,
    pause_dynamic_pricing as _pause_dynamic_pricing,
    delete_dynamic_pricing as _delete_dynamic_pricing,
)
from services.chatbot_svc.tools.toggle_settings import compute_disable_counts
from services.chatbot_svc.schemas import (
    PaneConfigInput, ApplyPaneConfigResult, PauseDynamicPricingResult, DeleteDynamicPricingResult,
)

logger = structlog.get_logger(__name__)


def apply_dynamic_pricing_config(
    shop_domain: str, product_id: str, config: PaneConfigInput
) -> ApplyPaneConfigResult:
    try:
        with get_db() as s:
            product = s.get(models.ShopifyProduct, product_id)
            if product is None or product.shopDomain != shop_domain:
                logger.warning(
                    "dynamic_pricing_product_not_found",
                    shop_domain=shop_domain, product_id=product_id, action="apply",
                )
                raise RuntimeError(
                    f"Product {product_id} not found in this shop. "
                    f"Resolve it with resolve_product first."
                )

            previously_configured = False
            if not product.dynamicPricingEnabled:
                # "Ever configured before" signal. frequencyUnit survives a CHAT
                # pause (pause_dynamic_pricing only flips the flag) but NOT a
                # BROWSER pause (app.products.jsx's toggleDynamic OFF branch
                # explicitly nulls frequencyUnit/frequencyInterval and resets
                # pricingTier to COMPETITIVE) — so also check for real scrape
                # history (a ProductUrl row), which neither pause path touches
                # and only delete_dynamic_pricing removes. Without this, a
                # product paused in the browser then resumed via chat would be
                # wrongly treated as never-configured.
                has_history = (
                    s.query(models.ProductUrl.id)
                    .filter(models.ProductUrl.shopifyProductId == product_id)
                    .first()
                    is not None
                )
                previously_configured = (
                    product.frequencyUnit is not None
                    or product.frequencyInterval is not None
                    or has_history
                )
                missing = []
                if config.pricing_tier is None and not previously_configured:
                    missing.append("pricing tier (BUDGET, COMPETITIVE, or PREMIUM)")
                unit_missing = config.frequency_unit is None and product.frequencyUnit is None
                interval_missing = config.frequency_interval is None and product.frequencyInterval is None
                if (unit_missing or interval_missing) and not previously_configured:
                    missing.append("rescrape frequency (both a unit and a number, e.g. every 6 hours)")
                if missing:
                    logger.warning(
                        "dynamic_pricing_apply_missing_fields",
                        shop_domain=shop_domain, product_id=product_id, missing=missing,
                    )
                    raise RuntimeError(
                        f"{product.title} isn't tracking dynamic pricing yet. Turning it on for "
                        f"the first time needs: {'; '.join(missing)}. Ask the merchant for the "
                        f"missing value(s), then call this tool again with the complete config."
                    )

            # A previously-configured product whose frequency was wiped by a
            # browser pause (see previously_configured above) must not resume
            # with a permanently-null schedule — apply_pane_config's own
            # "config value or existing value" fallback would leave it null
            # forever, so the beat scheduler's frequencyUnit <> 'never' filter
            # would silently never match. Fall back to the shop's default
            # cadence instead, mirroring app.products.jsx's own toggleDynamic
            # ON-branch fallback (copy ShopSettings when nothing is on file).
            effective_frequency_unit = config.frequency_unit
            effective_frequency_interval = config.frequency_interval
            if (
                previously_configured
                and config.frequency_unit is None
                and product.frequencyUnit is None
            ):
                settings = s.get(models.ShopSettings, shop_domain)
                if settings is not None:
                    effective_frequency_unit = settings.frequencyUnit
                    effective_frequency_interval = settings.frequencyInterval

            pane_config = PaneConfig(
                search_query_override=config.search_query_override,
                pricing_tier=config.pricing_tier,
                min_price_override=config.min_price_override,
                max_price_override=config.max_price_override,
                frequency_unit=effective_frequency_unit,
                frequency_interval=effective_frequency_interval,
                discovery_num_results=config.discovery_num_results,
                listing_expansion_cap=config.listing_expansion_cap,
            )
            try:
                result = apply_pane_config(s, product, pane_config)
            except PaneConfigError as exc:
                logger.warning(
                    "dynamic_pricing_apply_invalid_config",
                    shop_domain=shop_domain, product_id=product_id, error=str(exc),
                )
                raise RuntimeError(str(exc)) from exc

            before, after = result["dynamicPricingEnabled"]
            logger.info(
                "dynamic_pricing_applied",
                shop_domain=shop_domain, product_id=product_id,
                pricing_tier=product.pricingTier,
                frequency_unit=effective_frequency_unit,
                frequency_interval=effective_frequency_interval,
                rearmed_count=result["rearmedCount"],
                enabled_before=before, enabled_after=after,
            )
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
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error(
            "dynamic_pricing_apply_failed",
            shop_domain=shop_domain, product_id=product_id, error=str(exc),
            exc_info=True,
        )
        raise RuntimeError(
            f"Something went wrong applying dynamic pricing for this product. "
            f"Please try again."
        ) from exc


def pause_dynamic_pricing(shop_domain: str, product_id: str) -> PauseDynamicPricingResult:
    try:
        with get_db() as s:
            product = s.get(models.ShopifyProduct, product_id)
            if product is None or product.shopDomain != shop_domain:
                logger.warning(
                    "dynamic_pricing_product_not_found",
                    shop_domain=shop_domain, product_id=product_id, action="pause",
                )
                raise RuntimeError(
                    f"Product {product_id} not found in this shop. "
                    f"Resolve it with resolve_product first."
                )

            result = _pause_dynamic_pricing(s, product)
            before = result["dynamicPricingEnabled"]["old"]
            after = result["dynamicPricingEnabled"]["new"]

            logger.info(
                "dynamic_pricing_paused",
                shop_domain=shop_domain, product_id=product_id,
                enabled_before=before, enabled_after=after,
            )
            return PauseDynamicPricingResult(
                product_id=product.id,
                product_title=product.title,
                dynamic_pricing_enabled_before=before,
                dynamic_pricing_enabled_after=after,
                human_summary=f"Dynamic pricing is now paused for {product.title}.",
            )
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error(
            "dynamic_pricing_pause_failed",
            shop_domain=shop_domain, product_id=product_id, error=str(exc),
            exc_info=True,
        )
        raise RuntimeError(
            f"Something went wrong pausing dynamic pricing for this product. "
            f"Please try again."
        ) from exc


def get_delete_preview(shop_domain: str, product_id: str) -> dict:
    try:
        return compute_disable_counts(shop_domain, product_id)
    except Exception as exc:
        logger.error(
            "dynamic_pricing_delete_preview_failed",
            shop_domain=shop_domain, product_id=product_id, error=str(exc),
            exc_info=True,
        )
        raise RuntimeError(
            f"Something went wrong previewing the delete for this product. "
            f"Please try again."
        ) from exc


def delete_dynamic_pricing(shop_domain: str, product_id: str, confirmed: bool) -> DeleteDynamicPricingResult:
    try:
        with get_db() as s:
            product = s.get(models.ShopifyProduct, product_id)
            if product is None or product.shopDomain != shop_domain:
                logger.warning(
                    "dynamic_pricing_product_not_found",
                    shop_domain=shop_domain, product_id=product_id, action="delete",
                )
                raise RuntimeError(
                    f"Product {product_id} not found in this shop. "
                    f"Resolve it with resolve_product first."
                )

            if not confirmed:
                logger.warning(
                    "dynamic_pricing_delete_not_confirmed",
                    shop_domain=shop_domain, product_id=product_id,
                )
                raise RuntimeError(
                    "Deletion is permanent and was not confirmed. Warn the merchant with "
                    "the counts from get_delete_preview and ask via ask_user first; only "
                    "call this tool again with confirmed=True after they explicitly agree."
                )

            result = _delete_dynamic_pricing(s, product)

            logger.info(
                "dynamic_pricing_deleted",
                shop_domain=shop_domain, product_id=product_id,
                deleted_scraped_products=result["deletedScrapedProducts"],
            )
            return DeleteDynamicPricingResult(
                product_id=product.id,
                product_title=product.title,
                deleted_scraped_products=result["deletedScrapedProducts"],
                human_summary=(
                    f"Deleted all dynamic-pricing data for {product.title} "
                    f"({result['deletedScrapedProducts']} competitor product(s) removed)."
                ),
            )
    except RuntimeError:
        raise
    except Exception as exc:
        logger.error(
            "dynamic_pricing_delete_failed",
            shop_domain=shop_domain, product_id=product_id, error=str(exc),
            exc_info=True,
        )
        raise RuntimeError(
            f"Something went wrong deleting dynamic-pricing data for this product. "
            f"Please try again."
        ) from exc
