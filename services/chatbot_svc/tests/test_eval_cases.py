from services.chatbot_svc.evals.cases import build_cases, shop_price_set


def test_shop_price_set_collects_all_variant_prices(seed_shop):
    assert shop_price_set(seed_shop) == {99.0}


def test_build_cases_produces_price_query_per_product(seed_shop):
    cases = build_cases(seed_shop)
    # one seeded product -> at least the per-product price case + generic cases
    price_cases = [c for c in cases if c.name.startswith("price_query_")]
    assert len(price_cases) == 1
    meta = price_cases[0].metadata
    assert meta["expected_facts"][0] == "99"
    assert meta["expected_tools"] == ["resolve_product"]
    assert 99.0 in meta["allowed_prices"]


def test_build_cases_includes_generic_cases(seed_shop):
    names = {c.name for c in build_cases(seed_shop)}
    assert {"nonexistent_product", "toggle_enable_first", "price_increase_preview",
            "stats_average_price", "no_price_history"} <= names
