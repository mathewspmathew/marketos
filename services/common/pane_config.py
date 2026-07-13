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
