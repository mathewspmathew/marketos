"""
services/common/pane_config.py

Pure-Python equivalent of the "Save & Enable Dynamic Pricing" action in
shopify_ui/app/routes/app.products.jsx (intent === "saveAndEnable"). Lets
scripts (e.g. scripts/smoke_pipeline.py) configure a ShopifyProduct's
dynamic-pricing pane fields without going through Node/Prisma-JS/the browser.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from services.common import models
from services.common.frequency import InvalidFrequency, next_run_at, validate_interval, validate_unit

PRICING_TIERS = ("BUDGET", "COMPETITIVE", "PREMIUM")


class PaneConfigError(ValueError):
    """Raised when pane config inputs fail validation. Caller must not write to the DB."""


@dataclass
class PaneConfig:
    search_query_override: Optional[str] = None
    pricing_tier: Optional[str] = None
    min_price_override: Optional[float] = None
    max_price_override: Optional[float] = None
    frequency_unit: Optional[str] = None
    frequency_interval: Optional[int] = None
    discovery_num_results: Optional[int] = None
    listing_expansion_cap: Optional[int] = None


def validate_pane_config(config: PaneConfig) -> None:
    """Raise PaneConfigError if any provided field is invalid."""
    if config.pricing_tier is not None and config.pricing_tier not in PRICING_TIERS:
        raise PaneConfigError(f"pricingTier must be one of {PRICING_TIERS}, got {config.pricing_tier!r}")

    if config.min_price_override is not None and config.min_price_override <= 0:
        raise PaneConfigError("Minimum price must be greater than 0.")
    if config.max_price_override is not None and config.max_price_override <= 0:
        raise PaneConfigError("Maximum price must be greater than 0.")
    if (
        config.min_price_override is not None
        and config.max_price_override is not None
        and config.min_price_override >= config.max_price_override
    ):
        raise PaneConfigError("Minimum price must be below the maximum.")

    if config.frequency_unit is not None:
        try:
            validate_unit(config.frequency_unit)
        except InvalidFrequency as exc:
            raise PaneConfigError(str(exc)) from exc
    if config.frequency_interval is not None:
        try:
            validate_interval(config.frequency_interval)
        except InvalidFrequency as exc:
            raise PaneConfigError(str(exc)) from exc

    if config.discovery_num_results is not None and config.discovery_num_results <= 0:
        raise PaneConfigError("discoveryNumResults must be a positive int.")
    if config.listing_expansion_cap is not None and config.listing_expansion_cap <= 0:
        raise PaneConfigError("listingExpansionCap must be a positive int.")


def apply_pane_config(session: Session, product: "models.ShopifyProduct", config: PaneConfig) -> dict:
    """Write `config`'s non-None fields onto `product`, enable dynamic pricing,
    and re-arm all ACTIVE ProductUrl rows for it. Raises PaneConfigError first
    if anything is invalid — no partial writes. Returns a summary dict.
    """
    validate_pane_config(config)

    changes: dict = {"dynamicPricingEnabled": (product.dynamicPricingEnabled, True)}

    if config.search_query_override is not None:
        product.searchQueryOverride = config.search_query_override or None
    if config.pricing_tier is not None:
        product.pricingTier = config.pricing_tier
    if config.min_price_override is not None:
        product.minPriceOverride = config.min_price_override
    if config.max_price_override is not None:
        product.maxPriceOverride = config.max_price_override
    if config.frequency_unit is not None:
        product.frequencyUnit = config.frequency_unit
    if config.frequency_interval is not None:
        product.frequencyInterval = config.frequency_interval
    if config.discovery_num_results is not None:
        product.discoveryNumResults = min(config.discovery_num_results, 50)
    if config.listing_expansion_cap is not None:
        product.listingExpansionCap = min(config.listing_expansion_cap, 50)

    product.dynamicPricingEnabled = True

    effective_unit = config.frequency_unit or product.frequencyUnit
    effective_interval = config.frequency_interval or product.frequencyInterval
    next_run = next_run_at(effective_interval, effective_unit) or datetime.now(timezone.utc)

    rearmed = (
        session.query(models.ProductUrl)
        .filter(
            models.ProductUrl.shopifyProductId == product.id,
            models.ProductUrl.status == "ACTIVE",
        )
        .update({"nextRunAt": next_run}, synchronize_session=False)
    )

    changes["nextRunAt"] = next_run
    changes["rearmedCount"] = rearmed
    return changes
