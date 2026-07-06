from services.chatbot_svc.evals.cases import build_cases, shop_price_set

EXPECTED_NAMES = [
    "price_query",
    "dp_status",
    "toggle_enable",
    "nonexistent_product",
    "ambiguous_reference",
]


def test_shop_price_set_collects_all_variant_prices(seed_shop):
    assert shop_price_set(seed_shop) == {99.0}


def test_build_cases_returns_exactly_five_fixed_names(seed_shop):
    cases = build_cases(seed_shop)
    assert [c.name for c in cases] == EXPECTED_NAMES


def test_price_query_uses_first_product_facts(seed_shop):
    case = {c.name: c for c in build_cases(seed_shop)}["price_query"]
    meta = case.metadata
    # seed fixture: "Boat Speaker White" at 99.0
    assert "Boat" in case.inputs
    assert meta["expected_facts"] == ["99"]
    assert meta["expected_tools"] == ["resolve_product"]
    assert 99.0 in meta["allowed_prices"]


def test_toggle_enable_requires_preview_rules(seed_shop):
    meta = {c.name: c for c in build_cases(seed_shop)}["toggle_enable"].metadata
    assert "preview_dynamic_pricing_toggle" in meta["expected_tools"]
    assert "toggle_needs_preview" in meta["rules"]
    assert "no_claim_applied" in meta["rules"]


def test_build_cases_empty_shop_returns_no_cases():
    assert build_cases("no-such-shop.myshopify.com") == []
