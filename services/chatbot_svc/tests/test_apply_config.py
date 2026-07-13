import os
os.environ.setdefault("GROQ_API_KEY", "test")

import uuid

import pydantic
import pytest

from services.common.db import get_db
from services.common.models import ShopifyProduct
from services.chatbot_svc.schemas import PaneConfigInput
from services.chatbot_svc.tools.apply_config import (
    apply_dynamic_pricing_config, pause_dynamic_pricing,
)


def _product_id(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(ShopifyProduct.shopDomain == shop).scalar()


def _blank_config(**overrides):
    """PaneConfigInput now requires every field explicitly (null = unmentioned
    by the user) — this builds an all-null config with the given overrides,
    matching what the LLM is required to send on every real call."""
    defaults = dict(
        search_query_override=None,
        pricing_tier=None,
        min_price_override=None,
        max_price_override=None,
        frequency_unit=None,
        frequency_interval=None,
        discovery_num_results=None,
        listing_expansion_cap=None,
    )
    defaults.update(overrides)
    return PaneConfigInput(**defaults)


def test_apply_writes_fields_and_enables(seed_shop):
    pid = _product_id(seed_shop)
    result = apply_dynamic_pricing_config(
        seed_shop, pid,
        _blank_config(
            pricing_tier="PREMIUM",
            min_price_override=800,
            max_price_override=1200,
            frequency_unit="hour",
            frequency_interval=6,
        ),
    )
    assert result.product_id == pid
    assert result.dynamic_pricing_enabled_before is False
    assert result.dynamic_pricing_enabled_after is True

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is True
        assert product.pricingTier == "PREMIUM"
        assert float(product.minPriceOverride) == 800.0
        assert float(product.maxPriceOverride) == 1200.0
        assert product.frequencyUnit == "hour"
        assert product.frequencyInterval == 6


def test_apply_rejects_product_from_another_shop(seed_shop, seed_other_shop):
    other_pid = _product_id(seed_other_shop)
    with pytest.raises(RuntimeError):
        apply_dynamic_pricing_config(
            seed_shop, other_pid, _blank_config(pricing_tier="BUDGET"),
        )

    with get_db() as s:
        product = s.get(ShopifyProduct, other_pid)
        assert product.dynamicPricingEnabled is False
        assert product.pricingTier == "COMPETITIVE"  # untouched


def test_apply_invalid_bounds_raises_runtime_error_not_pane_config_error(seed_shop):
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError):
        apply_dynamic_pricing_config(
            seed_shop, pid,
            _blank_config(min_price_override=100, max_price_override=50),
        )

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False  # no partial write


def test_pane_config_input_rejects_unrecognized_field_names():
    """Regression test: a live run showed the LLM can call this tool with
    plausible-but-wrong field names (e.g. "tier"/"min_price" instead of
    "pricing_tier"/"min_price_override"). Before this test was added,
    PaneConfigInput silently dropped unknown keys and validated as an
    all-null config — the tool call "succeeded" with nothing actually
    applied, while the agent still reported false specifics to the
    merchant. extra="forbid" must turn that into a loud ValidationError."""
    with pytest.raises(pydantic.ValidationError):
        PaneConfigInput(tier="PREMIUM", min_price=800)  # wrong names entirely


def test_pane_config_input_requires_every_field_present():
    """Every field must be explicitly present (as a real value or null) —
    an LLM that omits a field entirely (rather than sending it as null)
    must get a validation error, not a silent default."""
    with pytest.raises(pydantic.ValidationError):
        PaneConfigInput(pricing_tier="PREMIUM")  # missing the other 7 fields


def test_apply_omitted_fields_leave_existing_values(seed_shop):
    pid = _product_id(seed_shop)
    # First enable (tier + frequency required) before testing that a later
    # partial update leaves other fields untouched — the omitted-fields
    # semantics this test targets apply to already-active updates, which
    # the first-enable required-field guard doesn't gate (see
    # test_update_on_already_active_product_is_not_gated).
    apply_dynamic_pricing_config(
        seed_shop, pid,
        _blank_config(pricing_tier="COMPETITIVE", frequency_unit="day", frequency_interval=1),
    )
    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        product.searchQueryOverride = "existing override"
        s.flush()

    apply_dynamic_pricing_config(seed_shop, pid, _blank_config(pricing_tier="BUDGET"))

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.searchQueryOverride == "existing override"
        assert product.pricingTier == "BUDGET"


def test_pause_disables_flag_and_keeps_config(seed_shop):
    pid = _product_id(seed_shop)
    apply_dynamic_pricing_config(
        seed_shop, pid,
        _blank_config(
            pricing_tier="PREMIUM",
            min_price_override=800,
            max_price_override=1200,
            frequency_unit="hour",
            frequency_interval=6,
        ),
    )

    result = pause_dynamic_pricing(seed_shop, pid)
    assert result.product_id == pid
    assert result.dynamic_pricing_enabled_before is True
    assert result.dynamic_pricing_enabled_after is False

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False
        # Config must survive the pause untouched.
        assert product.pricingTier == "PREMIUM"
        assert float(product.minPriceOverride) == 800.0
        assert float(product.maxPriceOverride) == 1200.0
        assert product.frequencyUnit == "hour"
        assert product.frequencyInterval == 6


def test_pause_rejects_product_from_another_shop(seed_shop, seed_other_shop):
    other_pid = _product_id(seed_other_shop)
    with pytest.raises(RuntimeError):
        pause_dynamic_pricing(seed_shop, other_pid)

    with get_db() as s:
        product = s.get(ShopifyProduct, other_pid)
        assert product.dynamicPricingEnabled is False  # untouched (was already False)


def test_first_enable_missing_tier_is_rejected(seed_shop):
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError, match="pricing tier"):
        apply_dynamic_pricing_config(
            seed_shop, pid,
            _blank_config(frequency_unit="hour", frequency_interval=6),
        )

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False  # no partial write


def test_first_enable_missing_frequency_is_rejected(seed_shop):
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError, match="rescrape frequency"):
        apply_dynamic_pricing_config(
            seed_shop, pid,
            _blank_config(pricing_tier="PREMIUM"),
        )

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False


def test_first_enable_missing_both_names_both_in_error(seed_shop):
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError) as exc_info:
        apply_dynamic_pricing_config(seed_shop, pid, _blank_config())
    assert "pricing tier" in str(exc_info.value)
    assert "rescrape frequency" in str(exc_info.value)


def test_first_enable_with_tier_and_frequency_succeeds_other_fields_optional(seed_shop):
    pid = _product_id(seed_shop)
    result = apply_dynamic_pricing_config(
        seed_shop, pid,
        _blank_config(pricing_tier="BUDGET", frequency_unit="day", frequency_interval=1),
    )
    assert result.dynamic_pricing_enabled_after is True

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is True
        assert product.pricingTier == "BUDGET"
        assert product.frequencyUnit == "day"
        assert product.frequencyInterval == 1


def test_update_on_already_active_product_is_not_gated(seed_shop):
    pid = _product_id(seed_shop)
    # First enable with tier + frequency (satisfies the gate).
    apply_dynamic_pricing_config(
        seed_shop, pid,
        _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
    )

    # Follow-up tweak with tier/frequency omitted — must NOT be gated,
    # since the product is already active.
    result = apply_dynamic_pricing_config(
        seed_shop, pid,
        _blank_config(min_price_override=500),
    )
    assert result.dynamic_pricing_enabled_after is True

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert float(product.minPriceOverride) == 500.0
        # Existing tier/frequency survive untouched (apply_pane_config's
        # existing "None = unchanged" semantics, unaffected by this plan).
        assert product.pricingTier == "PREMIUM"
        assert product.frequencyUnit == "hour"
        assert product.frequencyInterval == 6
