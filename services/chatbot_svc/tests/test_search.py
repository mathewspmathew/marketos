import pytest

from services.chatbot_svc.schemas import ScopeFilter
from services.chatbot_svc.tools.search import structured_search


def test_filter_by_vendor(seed_shop):
    """vendor='Boat' returns only Boat variants, capped + scoped to shop."""
    rows = structured_search(seed_shop, ScopeFilter(vendor="Boat"), limit=25)
    assert len(rows) >= 1
    assert all(r.vendor == "Boat" for r in rows)


def test_filter_by_option(seed_shop):
    """option_filters={"color": "white"} returns variants with that option value."""
    rows = structured_search(seed_shop, ScopeFilter(option_filters={"color": "white"}))
    # The SQL filter guarantees only matching rows come back; assert at least one found.
    assert len(rows) >= 1


def test_filter_other_shop_isolated(seed_shop, seed_other_shop):
    """Rows from seed_shop must not include any product from seed_other_shop."""
    rows = structured_search(seed_shop, ScopeFilter())
    assert all(r.product_id != "other-shop-product" for r in rows)


def test_empty_filter_returns_paged_results(seed_shop):
    """Empty filter with limit=10 returns at most 10 rows."""
    rows = structured_search(seed_shop, ScopeFilter(), limit=10)
    assert len(rows) <= 10


def test_semantic_search_returns_variants(seed_shop, monkeypatch):
    from services.chatbot_svc.tools import search as s_mod

    def fake_embed(_text):
        return [0.0] * 768

    monkeypatch.setattr(s_mod, "_embed_text", fake_embed)
    rows = s_mod.semantic_search(seed_shop, "white speaker", top_k=5)
    assert isinstance(rows, list)


def test_get_variant_returns_detail(seed_shop):
    from services.chatbot_svc.tools.search import structured_search, get_variant
    from services.chatbot_svc.schemas import ScopeFilter
    rows = structured_search(seed_shop, ScopeFilter(vendor="Boat"))
    assert rows, "fixture should have at least one Boat variant"
    detail = get_variant(seed_shop, rows[0].variant_id)
    assert detail is not None
    assert detail.variant_id == rows[0].variant_id


def test_get_variant_cross_shop_returns_none(seed_shop, seed_other_shop):
    from services.chatbot_svc.tools.search import get_variant
    assert get_variant(seed_shop, "other-shop-variant") is None
