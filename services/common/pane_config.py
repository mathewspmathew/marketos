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
