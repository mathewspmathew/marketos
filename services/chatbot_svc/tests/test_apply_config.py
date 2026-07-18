import os
os.environ.setdefault("GROQ_API_KEY", "test")

import uuid

import pydantic
import pytest

from services.common.db import get_db
from services.common.models import ShopifyProduct, ChatSession
from services.common.models import CompetitorCandidate, ScrapedProduct, ProductUrl, ShopSettings
from services.chatbot_svc.schemas import PaneConfigInput
from services.chatbot_svc.tools.apply_config import (
    apply_dynamic_pricing_config, pause_dynamic_pricing, resume_dynamic_pricing,
    get_delete_preview, delete_dynamic_pricing,
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
        clear_min_price_override=False,
        clear_max_price_override=False,
    )
    defaults.update(overrides)
    return PaneConfigInput(**defaults)


def test_apply_writes_fields_and_enables(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    result = apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
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


def test_apply_rejects_product_from_another_shop(seed_shop, seed_other_shop, chat_session_id):
    other_pid = _product_id(seed_other_shop)
    with pytest.raises(RuntimeError):
        apply_dynamic_pricing_config(
            seed_shop, other_pid, chat_session_id, _blank_config(pricing_tier="BUDGET"),
        )

    with get_db() as s:
        product = s.get(ShopifyProduct, other_pid)
        assert product.dynamicPricingEnabled is False
        assert product.pricingTier == "COMPETITIVE"  # untouched


def test_apply_invalid_bounds_raises_runtime_error_not_pane_config_error(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError):
        apply_dynamic_pricing_config(
            seed_shop, pid, chat_session_id,
            _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6, min_price_override=100, max_price_override=50),
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


def test_apply_omitted_fields_leave_existing_values(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    # First enable (tier + frequency required) before testing that a later
    # partial update leaves other fields untouched — the omitted-fields
    # semantics this test targets apply to already-active updates, which
    # the first-enable required-field guard doesn't gate (see
    # test_update_on_already_active_product_is_not_gated).
    apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="COMPETITIVE", frequency_unit="day", frequency_interval=1),
    )
    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        product.searchQueryOverride = "existing override"
        s.flush()

    apply_dynamic_pricing_config(seed_shop, pid, chat_session_id, _blank_config(pricing_tier="BUDGET"))

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.searchQueryOverride == "existing override"
        assert product.pricingTier == "BUDGET"


def test_pause_disables_flag_and_keeps_config(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(
            pricing_tier="PREMIUM",
            min_price_override=800,
            max_price_override=1200,
            frequency_unit="hour",
            frequency_interval=6,
        ),
    )

    result = pause_dynamic_pricing(seed_shop, pid, chat_session_id)
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


def test_pause_rejects_product_from_another_shop(seed_shop, seed_other_shop, chat_session_id):
    other_pid = _product_id(seed_other_shop)
    with pytest.raises(RuntimeError):
        pause_dynamic_pricing(seed_shop, other_pid, chat_session_id)

    with get_db() as s:
        product = s.get(ShopifyProduct, other_pid)
        assert product.dynamicPricingEnabled is False  # untouched (was already False)


def test_resume_flips_flag_without_gate(seed_shop):
    pid = _product_id(seed_shop)
    result = resume_dynamic_pricing(seed_shop, pid)
    assert result.dynamic_pricing_enabled_before is False
    assert result.dynamic_pricing_enabled_after is True

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is True


def test_resume_unknown_product_raises(seed_shop):
    with pytest.raises(RuntimeError, match="not found"):
        resume_dynamic_pricing(seed_shop, "nonexistent-id")


def test_first_enable_missing_tier_is_rejected(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError, match="pricing tier"):
        apply_dynamic_pricing_config(
            seed_shop, pid, chat_session_id,
            _blank_config(frequency_unit="hour", frequency_interval=6),
        )

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False  # no partial write


def test_first_enable_missing_frequency_is_rejected(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError, match="rescrape frequency"):
        apply_dynamic_pricing_config(
            seed_shop, pid, chat_session_id,
            _blank_config(pricing_tier="PREMIUM"),
        )

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False


def test_first_enable_missing_both_names_both_in_error(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError) as exc_info:
        apply_dynamic_pricing_config(seed_shop, pid, chat_session_id, _blank_config())
    assert "pricing tier" in str(exc_info.value)
    assert "rescrape frequency" in str(exc_info.value)


def test_first_enable_with_tier_and_frequency_succeeds_other_fields_optional(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    result = apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="BUDGET", frequency_unit="day", frequency_interval=1),
    )
    assert result.dynamic_pricing_enabled_after is True

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is True
        assert product.pricingTier == "BUDGET"
        assert product.frequencyUnit == "day"
        assert product.frequencyInterval == 1


def test_update_on_already_active_product_is_not_gated(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    # First enable with tier + frequency (satisfies the gate).
    apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
    )

    # Follow-up tweak with tier/frequency omitted — must NOT be gated,
    # since the product is already active.
    result = apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
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


def test_apply_threads_clear_min_price_override_flag(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6, min_price_override=500),
    )
    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert float(product.minPriceOverride) == 500.0

    apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(clear_min_price_override=True),
    )
    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.minPriceOverride is None


def test_paused_product_partial_reconfigure_not_gated_on_frequency(seed_shop, chat_session_id):
    """A paused product (dynamicPricingEnabled=False) already has a frequency
    on file — the first-enable guard must not treat that as "missing" just
    because this message doesn't repeat it. Regression test for a gap found
    in the final whole-branch review: the guard originally checked only the
    incoming config, not the product's existing DB value, so a paused
    product's partial reconfigure was wrongly forced through the ask-for-
    frequency gate even though the frequency was already configured."""
    pid = _product_id(seed_shop)
    # Enable, then pause — leaves dynamicPricingEnabled=False with tier/frequency intact.
    apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
    )
    pause_dynamic_pricing(seed_shop, pid, chat_session_id)

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False  # confirms this is the paused case

    # Tier-only update, frequency omitted from this message — must NOT raise,
    # since the product's existing frequency (hour/6) satisfies the guard.
    result = apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="BUDGET"),
    )
    assert result.dynamic_pricing_enabled_after is True

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.pricingTier == "BUDGET"
        assert product.frequencyUnit == "hour"
        assert product.frequencyInterval == 6


def test_delete_without_confirmation_is_rejected(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError, match="not confirmed"):
        delete_dynamic_pricing(seed_shop, pid, chat_session_id, confirmed=False)


def test_delete_rejects_product_from_another_shop(seed_shop, seed_other_shop, chat_session_id):
    other_pid = _product_id(seed_other_shop)
    with pytest.raises(RuntimeError):
        delete_dynamic_pricing(seed_shop, other_pid, chat_session_id, confirmed=True)


def test_delete_with_confirmation_removes_data(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    scraped_id = str(uuid.uuid4())
    cand_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(ScrapedProduct(id=scraped_id, shopDomain=seed_shop, domain="example.com", title="Competitor"))
        s.flush()
        s.add(CompetitorCandidate(
            id=cand_id, shopDomain=seed_shop, shopifyProductId=pid,
            url=f"https://example.com/{cand_id}", domain="example.com", source="serper",
            scrapedProductId=scraped_id,
        ))

    result = delete_dynamic_pricing(seed_shop, pid, chat_session_id, confirmed=True)
    assert result.product_id == pid
    assert result.deleted_scraped_products == 1

    with get_db() as s:
        assert s.get(ScrapedProduct, scraped_id) is None
        assert s.get(CompetitorCandidate, cand_id) is None
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False
        assert product.pricingTier == "COMPETITIVE"  # reset to default


def test_get_delete_preview_returns_real_counts(seed_shop):
    pid = _product_id(seed_shop)
    scraped_id = str(uuid.uuid4())
    cand_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(ScrapedProduct(id=scraped_id, shopDomain=seed_shop, domain="example.com", title="Competitor"))
        s.flush()
        s.add(CompetitorCandidate(
            id=cand_id, shopDomain=seed_shop, shopifyProductId=pid,
            url=f"https://example.com/{cand_id}", domain="example.com", source="serper",
            scrapedProductId=scraped_id,
        ))

    try:
        counts = get_delete_preview(seed_shop, pid)
        assert counts["competitor_products"] == 1
        assert counts["discovered_links"] == 1
        assert "price_stats_variants" in counts
    finally:
        with get_db() as s:
            s.query(CompetitorCandidate).filter(CompetitorCandidate.id == cand_id).delete(synchronize_session=False)
            s.query(ScrapedProduct).filter(ScrapedProduct.id == scraped_id).delete(synchronize_session=False)


def test_plain_resume_all_null_config_succeeds_on_previously_configured_product(seed_shop, chat_session_id):
    """A paused product (dynamicPricingEnabled=False) that was configured
    before (frequencyUnit already set) must accept a plain resume — an
    all-null config — without being asked to repeat a tier it already has.
    pricingTier can never be None (NOT NULL DB default), so the guard must
    use frequencyUnit-already-set as the "previously configured" signal
    to rescue tier too, not just frequency."""
    pid = _product_id(seed_shop)
    apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
    )
    pause_dynamic_pricing(seed_shop, pid, chat_session_id)

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False  # confirms paused, not fresh

    result = apply_dynamic_pricing_config(seed_shop, pid, chat_session_id, _blank_config())
    assert result.dynamic_pricing_enabled_after is True

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is True
        assert product.pricingTier == "PREMIUM"
        assert product.frequencyUnit == "hour"
        assert product.frequencyInterval == 6


def test_fresh_product_still_requires_tier_even_with_frequency_given(seed_shop, chat_session_id):
    """A truly never-configured product (frequencyUnit is None) must still
    require an explicit tier, even if this message gives a frequency —
    the previously_configured rescue must not fire for a genuine first-time
    enable."""
    pid = _product_id(seed_shop)
    with pytest.raises(RuntimeError, match="pricing tier"):
        apply_dynamic_pricing_config(
            seed_shop, pid, chat_session_id,
            _blank_config(frequency_unit="hour", frequency_interval=6),
        )


def test_paused_product_resumes_via_configured_at_not_history(seed_shop, chat_session_id):
    """Regression test, updated for the new dynamicPricingConfiguredAt
    signal (replaces the old ProductUrl-history heuristic this test used
    to exercise — see docs/superpowers/specs/2026-07-14-pricing-tier-configured-signal-design.md).
    A product with NO ProductUrl scrape history at all, but a real
    dynamicPricingConfiguredAt timestamp (set by a prior apply), must still
    resume without being asked for a tier again. Also confirms frequency
    still gets filled from ShopSettings when the product's own frequency
    was wiped (mirrors the browser pause path's behavior, unrelated to the
    signal-source change in this task)."""
    pid = _product_id(seed_shop)
    # First real configure — sets dynamicPricingConfiguredAt.
    apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
    )

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingConfiguredAt is not None
        # Simulate a browser pause that wipes frequency/tier but does NOT
        # touch dynamicPricingConfiguredAt (matches app.products.jsx's real
        # OFF-branch behavior, which this plan does not modify).
        product.dynamicPricingEnabled = False
        product.frequencyUnit = None
        product.frequencyInterval = None
        product.pricingTier = "PREMIUM"
        s.flush()

    with get_db() as s:
        s.add(ShopSettings(shopDomain=seed_shop, frequencyUnit="hour", frequencyInterval=4, defaultPricingTier="BUDGET"))

    try:
        result = apply_dynamic_pricing_config(seed_shop, pid, chat_session_id, _blank_config())
        assert result.dynamic_pricing_enabled_after is True

        with get_db() as s:
            product = s.get(ShopifyProduct, pid)
            assert product.dynamicPricingEnabled is True
            assert product.frequencyUnit == "hour"
            assert product.frequencyInterval == 4
            assert product.pricingTier == "PREMIUM"
    finally:
        with get_db() as s:
            s.query(ShopSettings).filter(ShopSettings.shopDomain == seed_shop).delete(synchronize_session=False)


def test_fresh_product_uses_shop_default_tier_instead_of_asking(seed_shop, chat_session_id):
    """A genuinely never-configured product (dynamicPricingConfiguredAt is
    None) with a ShopSettings row present must use
    ShopSettings.defaultPricingTier instead of raising the missing-tier
    error, as long as frequency is also given."""
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.add(ShopSettings(shopDomain=seed_shop, defaultPricingTier="PREMIUM"))

    try:
        result = apply_dynamic_pricing_config(
            seed_shop, pid, chat_session_id,
            _blank_config(frequency_unit="hour", frequency_interval=6),
        )
        assert result.dynamic_pricing_enabled_after is True

        with get_db() as s:
            product = s.get(ShopifyProduct, pid)
            assert product.pricingTier == "PREMIUM"
    finally:
        with get_db() as s:
            s.query(ShopSettings).filter(ShopSettings.shopDomain == seed_shop).delete(synchronize_session=False)


def test_fresh_product_with_no_shop_settings_still_asks_for_tier(seed_shop, chat_session_id):
    """A never-configured product with NO ShopSettings row at all (shop has
    never visited the Settings page) must still be asked for a tier — there
    is no fallback to use."""
    pid = _product_id(seed_shop)
    with get_db() as s:
        existing = s.get(ShopSettings, seed_shop)
        assert existing is None  # seed_shop fixture doesn't create one

    with pytest.raises(RuntimeError, match="pricing tier"):
        apply_dynamic_pricing_config(
            seed_shop, pid, chat_session_id,
            _blank_config(frequency_unit="hour", frequency_interval=6),
        )


def test_apply_rejects_unresolved_product(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.query(ChatSession).filter(ChatSession.id == chat_session_id).update(
            {"resolvedProductIds": []}, synchronize_session=False,
        )

    with pytest.raises(RuntimeError, match="hasn't been resolved"):
        apply_dynamic_pricing_config(
            seed_shop, pid, chat_session_id,
            _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
        )

    with get_db() as s:
        product = s.get(ShopifyProduct, pid)
        assert product.dynamicPricingEnabled is False  # no partial write


def test_apply_accepts_resolved_product(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    result = apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
    )
    assert result.dynamic_pricing_enabled_after is True


def test_pause_rejects_unresolved_product(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    apply_dynamic_pricing_config(
        seed_shop, pid, chat_session_id,
        _blank_config(pricing_tier="PREMIUM", frequency_unit="hour", frequency_interval=6),
    )
    with get_db() as s:
        s.query(ChatSession).filter(ChatSession.id == chat_session_id).update(
            {"resolvedProductIds": []}, synchronize_session=False,
        )

    with pytest.raises(RuntimeError, match="hasn't been resolved"):
        pause_dynamic_pricing(seed_shop, pid, chat_session_id)


def test_delete_rejects_unresolved_product(seed_shop, chat_session_id):
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.query(ChatSession).filter(ChatSession.id == chat_session_id).update(
            {"resolvedProductIds": []}, synchronize_session=False,
        )

    with pytest.raises(RuntimeError, match="hasn't been resolved"):
        delete_dynamic_pricing(seed_shop, pid, chat_session_id, confirmed=True)


def test_get_delete_preview_does_not_require_session_id(seed_shop):
    """get_delete_preview is deliberately NOT gated — confirms its
    signature is untouched by this task."""
    pid = _product_id(seed_shop)
    counts = get_delete_preview(seed_shop, pid)
    assert "competitor_products" in counts


def test_resume_dynamic_pricing_signature_is_untouched(seed_shop):
    """resume_dynamic_pricing is not a registered chat tool and is out of
    scope for this plan — confirms its signature has NOT grown a session_id
    parameter as a side effect of this task's mechanical edits."""
    pid = _product_id(seed_shop)
    result = resume_dynamic_pricing(seed_shop, pid)  # still 2 args, no session_id
    assert result.dynamic_pricing_enabled_after is True
