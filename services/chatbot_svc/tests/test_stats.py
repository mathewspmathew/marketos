from services.chatbot_svc.tools.stats import StatsMetric, get_stats


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


def test_recent_decisions_limited(seed_shop):
    res = get_stats(seed_shop, StatsMetric.recent_decisions)
    assert "items" in res
    assert len(res["items"]) <= 20


def test_suggestion_counts_returns_by_status_dict(seed_shop):
    res = get_stats(seed_shop, StatsMetric.suggestion_counts)
    assert "by_status" in res
    assert isinstance(res["by_status"], dict)
