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

from sqlalchemy import or_
from sqlalchemy.orm import Session

from services.common import models
from services.common.frequency import InvalidFrequency, next_run_at, validate_interval, validate_unit

PRICING_TIERS = ("BUDGET", "COMPETITIVE", "PREMIUM")


class PaneConfigError(ValueError):
    """Raised when pane config inputs fail validation, or when a product's
    first-ever enable is missing required fields. Caller must not write to
    the DB. `missing_fields` is set only for the latter case."""

    def __init__(self, message: str, missing_fields: Optional[list[str]] = None):
        super().__init__(message)
        self.missing_fields = missing_fields


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
    clear_min_price_override: bool = False
    clear_max_price_override: bool = False


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

    On a product's first-ever enable (dynamicPricingConfiguredAt was None
    going in), a missing pricing tier falls back to the shop's
    ShopSettings.defaultPricingTier before the "missing fields" gate runs;
    a missing frequency on an already-configured (but currently disabled)
    product falls back to the shop's frequency defaults. If tier or
    frequency are still missing after fallback on a first-ever enable,
    raises PaneConfigError with `missing_fields` populated instead of
    writing anything.
    """
    previously_configured = product.dynamicPricingConfiguredAt is not None
    is_first_configure = not previously_configured

    effective_pricing_tier = config.pricing_tier
    if effective_pricing_tier is None and not previously_configured:
        settings = session.get(models.ShopSettings, product.shopDomain)
        if settings is not None:
            effective_pricing_tier = settings.defaultPricingTier

    effective_frequency_unit = config.frequency_unit
    effective_frequency_interval = config.frequency_interval
    if (
        previously_configured
        and config.frequency_unit is None
        and product.frequencyUnit is None
    ):
        # A previously-configured product whose frequency is unset (e.g. a
        # browser-side pause from before dynamicPricingConfiguredAt existed)
        # must not resume with a permanently-null schedule.
        settings = session.get(models.ShopSettings, product.shopDomain)
        if settings is not None:
            effective_frequency_unit = settings.frequencyUnit
            effective_frequency_interval = settings.frequencyInterval

    if not previously_configured:
        missing = []
        if effective_pricing_tier is None:
            missing.append("pricing tier (BUDGET, COMPETITIVE, or PREMIUM)")
        unit_missing = config.frequency_unit is None and product.frequencyUnit is None
        interval_missing = config.frequency_interval is None and product.frequencyInterval is None
        if unit_missing or interval_missing:
            missing.append("rescrape frequency (both a unit and a number, e.g. every 6 hours)")
        if missing:
            raise PaneConfigError(
                f"{product.title} isn't tracking dynamic pricing yet. Turning it on for "
                f"the first time needs: {'; '.join(missing)}.",
                missing_fields=missing,
            )

    effective_min_price = None if config.clear_min_price_override else config.min_price_override
    effective_max_price = None if config.clear_max_price_override else config.max_price_override

    effective_config = PaneConfig(
        search_query_override=config.search_query_override,
        pricing_tier=effective_pricing_tier,
        min_price_override=effective_min_price,
        max_price_override=effective_max_price,
        frequency_unit=effective_frequency_unit,
        frequency_interval=effective_frequency_interval,
        discovery_num_results=config.discovery_num_results,
        listing_expansion_cap=config.listing_expansion_cap,
    )
    validate_pane_config(effective_config)

    changes: dict = {"dynamicPricingEnabled": (product.dynamicPricingEnabled, True)}

    if effective_config.search_query_override is not None:
        product.searchQueryOverride = effective_config.search_query_override or None
    if effective_config.pricing_tier is not None:
        product.pricingTier = effective_config.pricing_tier
    if config.clear_min_price_override:
        product.minPriceOverride = None
    elif effective_config.min_price_override is not None:
        product.minPriceOverride = effective_config.min_price_override
    if config.clear_max_price_override:
        product.maxPriceOverride = None
    elif effective_config.max_price_override is not None:
        product.maxPriceOverride = effective_config.max_price_override
    if effective_config.frequency_unit is not None:
        product.frequencyUnit = effective_config.frequency_unit
    if effective_config.frequency_interval is not None:
        product.frequencyInterval = effective_config.frequency_interval
    # Frozen once a product has ever been configured — changing scrape scope
    # after discovery has already run against the old scope doesn't make sense.
    if effective_config.discovery_num_results is not None and is_first_configure:
        product.discoveryNumResults = min(effective_config.discovery_num_results, 50)
    if effective_config.listing_expansion_cap is not None and is_first_configure:
        product.listingExpansionCap = min(effective_config.listing_expansion_cap, 50)

    if is_first_configure:
        for variant in product.variants:
            if variant.basePrice is None and variant.currentPrice is not None and float(variant.currentPrice) > 0:
                variant.basePrice = variant.currentPrice
        bases = [
            float(v.basePrice) for v in product.variants
            if v.basePrice is not None and float(v.basePrice) > 0
        ]
        if bases:
            product.avgBasePrice = sum(bases) / len(bases)

        discovery_query = product.searchQueryOverride or product.searchQuery
        if product.lastDiscoveryAt is None and discovery_query:
            session.add(models.DiscoveryJob(
                shopDomain=product.shopDomain,
                shopifyProductId=product.id,
                status="QUEUED",
                query=discovery_query,
            ))

    product.dynamicPricingEnabled = True
    if product.dynamicPricingConfiguredAt is None:
        product.dynamicPricingConfiguredAt = datetime.now(timezone.utc)

    effective_unit = effective_config.frequency_unit or product.frequencyUnit
    effective_interval = effective_config.frequency_interval or product.frequencyInterval
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


def pause_dynamic_pricing(session: Session, product: "models.ShopifyProduct") -> dict:
    """Pause DP but keep all pane config intact — just disable the flag.
    Mirrors app.products.jsx's pauseDynamic intent exactly. If frequency is
    set, rescraping stops on the next beat tick (celery_beat.py's
    WHERE dynamicPricingEnabled = TRUE filter simply stops matching this
    product — no ProductUrl rows are touched).
    """
    old = product.dynamicPricingEnabled
    product.dynamicPricingEnabled = False
    return {"dynamicPricingEnabled": {"old": old, "new": False}}


def resume_dynamic_pricing(session: Session, product: "models.ShopifyProduct") -> dict:
    """Resume a paused product: re-enable the flag and re-arm any ACTIVE
    ProductUrl rows whose schedule has gone stale (null or past nextRunAt).
    No gate/validation — mirrors app.products.jsx's resumeDynamic intent
    exactly. A paused product was already configured once before pausing,
    so re-validating on resume would only add friction, never catch a real
    problem.
    """
    old = product.dynamicPricingEnabled
    product.dynamicPricingEnabled = True

    next_run = next_run_at(product.frequencyInterval, product.frequencyUnit) or datetime.now(timezone.utc)
    rearmed = (
        session.query(models.ProductUrl)
        .filter(
            models.ProductUrl.shopifyProductId == product.id,
            models.ProductUrl.status == "ACTIVE",
            or_(
                models.ProductUrl.nextRunAt.is_(None),
                models.ProductUrl.nextRunAt <= datetime.now(timezone.utc),
            ),
        )
        .update({"nextRunAt": next_run}, synchronize_session=False)
    )
    return {"dynamicPricingEnabled": {"old": old, "new": True}, "rearmedCount": rearmed}


def delete_dynamic_pricing(session: Session, product: "models.ShopifyProduct") -> dict:
    """Guarded, full teardown of a product's competitor/pricing data, then
    reset its pane config to defaults. Merges the two existing JS
    implementations (deleteDynamicWithData in app.products.jsx +
    competitorTeardown.server.js::deleteCompetitorData — see
    docs/superpowers/specs/2026-07-13-pause-delete-dynamic-pricing-design.md
    for the discrepancy this reconciles): the shared-ScrapedProduct guard
    from the latter, the fuller cleanup from the former, minus the latter's
    ProductSuggestion call (that model does not exist in the schema).
    """
    product_id  = product.id
    shop_domain = product.shopDomain

    cand_scraped_ids = {
        row[0] for row in session.query(models.CompetitorCandidate.scrapedProductId)
        .filter(
            models.CompetitorCandidate.shopDomain == shop_domain,
            models.CompetitorCandidate.shopifyProductId == product_id,
            models.CompetitorCandidate.scrapedProductId.isnot(None),
        )
        .all()
    }
    url_scraped_ids = {
        row[0] for row in session.query(models.ProductUrl.prodId)
        .filter(
            models.ProductUrl.shopDomain == shop_domain,
            models.ProductUrl.shopifyProductId == product_id,
        )
        .all()
    }
    my_scraped_ids = cand_scraped_ids | url_scraped_ids

    deletable_scraped_ids = []
    for sid in my_scraped_ids:
        other_cand = (
            session.query(models.CompetitorCandidate.id)
            .filter(
                models.CompetitorCandidate.scrapedProductId == sid,
                models.CompetitorCandidate.shopifyProductId != product_id,
            )
            .first()
        )
        other_url = (
            session.query(models.ProductUrl.id)
            .filter(
                models.ProductUrl.prodId == sid,
                models.ProductUrl.shopifyProductId != product_id,
            )
            .first()
        )
        if not other_cand and not other_url:
            deletable_scraped_ids.append(sid)

    variant_ids = [
        row[0] for row in session.query(models.ShopifyVariant.id)
        .filter(models.ShopifyVariant.productId == product_id)
        .all()
    ]

    if variant_ids:
        session.query(models.VariantCompetitorStats).filter(
            models.VariantCompetitorStats.shopifyVariantId.in_(variant_ids)
        ).delete(synchronize_session=False)
        session.query(models.PriceDecision).filter(
            models.PriceDecision.shopifyVariantId.in_(variant_ids)
        ).delete(synchronize_session=False)
        session.query(models.ProductMatch).filter(
            models.ProductMatch.shopifyVariantId.in_(variant_ids)
        ).delete(synchronize_session=False)

    session.query(models.ProductLevelMatch).filter(
        models.ProductLevelMatch.shopifyProductId == product_id
    ).delete(synchronize_session=False)

    config_ids = [
        row[0] for row in session.query(models.ProductUrl.configId)
        .filter(
            models.ProductUrl.shopDomain == shop_domain,
            models.ProductUrl.shopifyProductId == product_id,
            models.ProductUrl.configId.isnot(None),
        )
        .all()
    ]

    session.query(models.CompetitorCandidate).filter(
        models.CompetitorCandidate.shopDomain == shop_domain,
        models.CompetitorCandidate.shopifyProductId == product_id,
    ).delete(synchronize_session=False)
    session.query(models.DiscoveryJob).filter(
        models.DiscoveryJob.shopDomain == shop_domain,
        models.DiscoveryJob.shopifyProductId == product_id,
    ).delete(synchronize_session=False)
    session.query(models.ProductUrl).filter(
        models.ProductUrl.shopDomain == shop_domain,
        models.ProductUrl.shopifyProductId == product_id,
    ).delete(synchronize_session=False)

    if config_ids:
        session.query(models.ScrapingConfig).filter(
            models.ScrapingConfig.id.in_(config_ids)
        ).delete(synchronize_session=False)

    if deletable_scraped_ids:
        # DB-level ON DELETE CASCADE (Prisma-defined FKs) handles
        # ScrapedVariant, CompetitorPriceObservation, ProductEmbedding, and
        # any remaining competitor-side ProductMatch rows for these ids.
        session.query(models.ScrapedProduct).filter(
            models.ScrapedProduct.id.in_(deletable_scraped_ids)
        ).delete(synchronize_session=False)

    product.dynamicPricingEnabled = False
    product.dynamicPricingConfiguredAt = None
    product.frequencyInterval     = None
    product.frequencyUnit         = None
    product.pricingTier           = "COMPETITIVE"
    product.minPriceOverride      = None
    product.maxPriceOverride      = None
    product.searchQueryOverride   = None
    product.listingExpansionCap   = None
    product.discoveryNumResults   = None
    product.avgBasePrice          = None

    return {"deletedScrapedProducts": len(deletable_scraped_ids)}
