from services.chatbot_svc.evals.cases import build_cases, shop_price_set


def test_shop_price_set_collects_all_variant_prices(seed_shop):
    assert shop_price_set(seed_shop) == {99.0}


def test_build_cases_returns_all_forty_cases(seed_shop):
    cases = build_cases(seed_shop)
    names = [c.name for c in cases]
    assert len(names) == 40
    assert len(set(names)) == 40  # all unique
    # spot-check names spanning the file, plus both conditional cases —
    # seed_shop's product has one variant and a non-empty vendor ("Boat"),
    # so build_cases' two conditional cases are always included here.
    for expected in [
        "price_query", "dp_status", "toggle_enable", "toggle_disable",
        "nonexistent_product", "ambiguous_reference",
        "get_variant_details", "structured_search_vendor_filter",
        "delete_requires_preview_and_confirmation",
    ]:
        assert expected in names


def test_price_query_uses_first_product_facts(seed_shop):
    case = {c.name: c for c in build_cases(seed_shop)}["price_query"]
    meta = case.metadata
    # seed fixture: "Boat Speaker White" at 99.0
    assert "Boat" in case.inputs
    # expected_facts is now judge prose (a criteria sentence), not an exact
    # keyword list — check the price still appears in it.
    assert len(meta["expected_facts"]) == 1
    assert "99" in meta["expected_facts"][0]
    assert meta["expected_tools"] == ["resolve_product"]
    assert 99.0 in meta["allowed_prices"]


def test_toggle_enable_applies_directly_no_panel(seed_shop):
    meta = {c.name: c for c in build_cases(seed_shop)}["toggle_enable"].metadata
    assert meta["expected_tools"] == ["resolve_product", "apply_dynamic_pricing_config"]
    assert meta["rules"] == []


def test_toggle_disable_pauses_directly_no_panel(seed_shop):
    meta = {c.name: c for c in build_cases(seed_shop)}["toggle_disable"].metadata
    assert meta["expected_tools"] == ["resolve_product", "pause_dynamic_pricing"]
    assert meta["rules"] == []


def test_delete_case_requires_preview_and_ask_before_delete(seed_shop):
    meta = {c.name: c for c in build_cases(seed_shop)}["delete_requires_preview_and_confirmation"].metadata
    assert meta["expected_tools"] == ["resolve_product", "get_delete_preview", "ask_user"]
    assert "delete_dynamic_pricing" in meta["forbidden_tools"]


def test_build_cases_empty_shop_returns_no_cases():
    assert build_cases("no-such-shop.myshopify.com") == []
