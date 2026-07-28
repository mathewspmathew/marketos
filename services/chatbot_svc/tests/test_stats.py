from services.chatbot_svc.tools.stats import StatsMetric, get_stats


def test_catalog_summary_counts_products_and_variants(seed_shop):
    # seed_shop fixture creates exactly one product with one variant.
    res = get_stats(seed_shop, StatsMetric.catalog_summary)
    assert res == {"products": 1, "variants": 1}


def test_catalog_summary_is_shop_scoped(seed_shop, seed_other_shop):
    # The other shop's product/variant must not leak into seed_shop's counts.
    res = get_stats(seed_shop, StatsMetric.catalog_summary)
    assert res["products"] == 1
    assert res["variants"] == 1


def test_priced_above_competitor_returns_count(seed_shop):
    res = get_stats(seed_shop, StatsMetric.priced_above_competitor)
    assert "count" in res
    assert isinstance(res["count"], int)


def test_priced_below_competitor_returns_count(seed_shop):
    res = get_stats(seed_shop, StatsMetric.priced_below_competitor)
    assert "count" in res
    assert isinstance(res["count"], int)


def test_match_coverage_returns_pair(seed_shop):
    res = get_stats(seed_shop, StatsMetric.match_coverage)
    assert "matched" in res and "total" in res
    assert isinstance(res["matched"], int)
    assert isinstance(res["total"], int)


def test_recent_decisions_shape(seed_shop):
    res = get_stats(seed_shop, StatsMetric.recent_decisions)
    assert res["window"] == "7 days"
    assert res["total_decisions"] == res["price_changes"] + res["no_op_reaffirmations"]
    assert len(res["sample_changes"]) <= 5
    if res["price_changes"] == 0:
        assert res["changed_price_range"] is None
    else:
        assert res["changed_price_range"]["min"] <= res["changed_price_range"]["max"]
