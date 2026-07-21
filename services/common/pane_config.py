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


def validate_pane_config(config: PaneConfig, product: "models.ShopifyProduct" = None) -> None:
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

    if (
        config.discovery_num_results is not None
        and product is not None
        and product.lastDiscoveryNumResults is not None
        and config.discovery_num_results < product.lastDiscoveryNumResults
    ):
        raise PaneConfigError(
            f"Number of products can't be decreased below {product.lastDiscoveryNumResults} — "
            "the competitors already found for this product aren't removed by lowering this number."
        )
    if (
        config.listing_expansion_cap is not None
        and product is not None
        and product.listingExpansionCap is not None
        and config.listing_expansion_cap < product.listingExpansionCap
    ):
        raise PaneConfigError(
            f"Max products per listing page can't be decreased below {product.listingExpansionCap} — "
            "lowering this doesn't undo anything already found."
        )


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
    validate_pane_config(effective_config, product)

    changes: dict = {"dynamicPricingEnabled": (product.dynamicPricingEnabled, True)}

    # Captured before any mutation below, so the re-arm gate at the end of
    # this function can tell "frequency actually changed" from "the frontend
    # resent the same frequency it always resends on every save" (the pane
    # includes frequency in every submit, not just ones that touch it).
    prior_frequency_unit = product.frequencyUnit
    prior_frequency_interval = product.frequencyInterval
    # Also captured before mutation: whether this call is re-enabling a
    # previously-paused, already-configured product (the "Save & Resume"
    # button) — that's the second of two distinct paths a merchant can take
    # to resume, and it must trigger a fresh discovery search under the same
    # conditions resume_dynamic_pricing does, since it may carry an edited
    # search query / discoveryNumResults that a plain resume never would.
    was_enabled_before_this_call = product.dynamicPricingEnabled

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
    # Locked while the product is actively pricing (dynamicPricingEnabled),
    # editable before first configure OR while paused — pause, adjust scope,
    # resume is the supported way to change how broadly this product searches.
    discovery_fields_editable = is_first_configure or not product.dynamicPricingEnabled
    if effective_config.discovery_num_results is not None and discovery_fields_editable:
        product.discoveryNumResults = min(effective_config.discovery_num_results, 50)
    if effective_config.listing_expansion_cap is not None and discovery_fields_editable:
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
            product.lastDiscoveryNumResults = product.discoveryNumResults
    elif not was_enabled_before_this_call:
        # Not a first-ever configure, and the product was paused going into
        # this call — this is the "Save & Resume" path (edit search
        # settings while paused, then save-and-re-enable in one click).
        # Must honor the same pending-search-change trigger a plain resume
        # (resume_dynamic_pricing, the row-level "Resume" button) does.
        _maybe_trigger_resume_discovery(session, product)

    product.dynamicPricingEnabled = True
    if product.dynamicPricingConfiguredAt is None:
        product.dynamicPricingConfiguredAt = datetime.now(timezone.utc)

    effective_unit = effective_config.frequency_unit or product.frequencyUnit
    effective_interval = effective_config.frequency_interval or product.frequencyInterval
    next_run = next_run_at(effective_interval, effective_unit) or datetime.now(timezone.utc)

    # Re-arming resets every ACTIVE ProductUrl's rescrape countdown to the
    # full interval — only do that when the frequency genuinely changed (or
    # this is the product's first-ever configure, which has no prior
    # schedule to preserve). Otherwise an unrelated save (bounds, search
    # query, ...) would silently push back the next real competitor check,
    # since the pane resends frequency on every submit regardless of
    # whether the merchant touched it.
    frequency_changed = (
        effective_unit != prior_frequency_unit
        or effective_interval != prior_frequency_interval
    )
    if is_first_configure or frequency_changed:
        rearmed = (
            session.query(models.ProductUrl)
            .filter(
                models.ProductUrl.shopifyProductId == product.id,
                models.ProductUrl.status == "ACTIVE",
            )
            .update({"nextRunAt": next_run}, synchronize_session=False)
        )
    else:
        rearmed = 0

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


def _maybe_trigger_resume_discovery(session: Session, product: "models.ShopifyProduct") -> None:
    """If the search query or discoveryNumResults changed since the last
    real discovery search, queue exactly one fresh DiscoveryJob using the
    current search query and the current TOTAL count (never a subtracted
    difference — the search API has no pagination, so re-searching for
    just the delta would re-fetch the same top results already found, not
    novel ones). Called from both apply_pane_config (when a paused,
    previously-configured product is saved back to enabled — the "Save &
    Resume" button) and resume_dynamic_pricing (a plain resume with no
    field edits, via the row-level "Resume" button) — these are the two
    distinct UI paths a merchant can take to re-enable a paused product,
    and both must honor a pending search-settings change identically.
    """
    current_query = product.searchQueryOverride or product.searchQuery
    if not current_query:
        return
    latest_job = (
        session.query(models.DiscoveryJob)
        .filter(models.DiscoveryJob.shopifyProductId == product.id)
        .order_by(models.DiscoveryJob.requestedAt.desc())
        .first()
    )
    query_changed = latest_job is None or latest_job.query != current_query
    count_increased = (
        product.lastDiscoveryNumResults is not None
        and product.discoveryNumResults is not None
        and product.discoveryNumResults > product.lastDiscoveryNumResults
    )
    if query_changed or count_increased:
        session.add(models.DiscoveryJob(
            shopDomain=product.shopDomain,
            shopifyProductId=product.id,
            status="QUEUED",
            query=current_query,
        ))
        product.lastDiscoveryNumResults = product.discoveryNumResults


def resume_dynamic_pricing(session: Session, product: "models.ShopifyProduct") -> dict:
    """Resume a paused product: re-enable the flag, re-arm any ACTIVE
    ProductUrl rows whose schedule has gone stale (null or past nextRunAt),
    and — if the search query or discoveryNumResults changed while paused —
    queue exactly one fresh DiscoveryJob using the current search query and
    the current TOTAL count (never a subtracted difference: the search API
    has no pagination, so re-searching for just the delta would re-fetch the
    same top results already found, not novel ones).
    """
    old = product.dynamicPricingEnabled
    product.dynamicPricingEnabled = True

    _maybe_trigger_resume_discovery(session, product)

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
    product.lastDiscoveryNumResults = None
    product.avgBasePrice          = None

    return {"deletedScrapedProducts": len(deletable_scraped_ids)}
