"""Unit tests for services.common.pane_config.validate_pane_config — pure
validation, no DB. Mirrors the checks in
shopify_ui/app/routes/app.products.jsx's saveAndEnable action (lines 239-289)."""
import pytest

from services.common.pane_config import PaneConfig, PaneConfigError, validate_pane_config


def test_empty_config_is_valid():
    validate_pane_config(PaneConfig())


def test_valid_bounds_pass():
    validate_pane_config(PaneConfig(min_price_override=10.0, max_price_override=20.0))


def test_min_price_zero_rejected():
    with pytest.raises(PaneConfigError, match="Minimum price must be greater than 0"):
        validate_pane_config(PaneConfig(min_price_override=0))


def test_max_price_zero_rejected():
    with pytest.raises(PaneConfigError, match="Maximum price must be greater than 0"):
        validate_pane_config(PaneConfig(max_price_override=0))


def test_min_greater_than_max_rejected():
    with pytest.raises(PaneConfigError, match="Minimum price must be below the maximum"):
        validate_pane_config(PaneConfig(min_price_override=100, max_price_override=50))


def test_min_equal_max_rejected():
    with pytest.raises(PaneConfigError, match="Minimum price must be below the maximum"):
        validate_pane_config(PaneConfig(min_price_override=50, max_price_override=50))


def test_invalid_pricing_tier_rejected():
    with pytest.raises(PaneConfigError, match="pricingTier must be one of"):
        validate_pane_config(PaneConfig(pricing_tier="LUXURY"))


def test_valid_pricing_tier_passes():
    validate_pane_config(PaneConfig(pricing_tier="PREMIUM"))


def test_invalid_frequency_unit_rejected():
    with pytest.raises(PaneConfigError):
        validate_pane_config(PaneConfig(frequency_unit="fortnight"))


def test_invalid_frequency_interval_rejected():
    with pytest.raises(PaneConfigError):
        validate_pane_config(PaneConfig(frequency_interval=0))


def test_zero_discovery_num_results_rejected():
    with pytest.raises(PaneConfigError, match="discoveryNumResults must be a positive int"):
        validate_pane_config(PaneConfig(discovery_num_results=0))


def test_zero_listing_expansion_cap_rejected():
    with pytest.raises(PaneConfigError, match="listingExpansionCap must be a positive int"):
        validate_pane_config(PaneConfig(listing_expansion_cap=0))
