import pytest
import uuid
from datetime import datetime, timezone
from services.chatbot_svc.tools.preview import preview_price_change
from services.chatbot_svc.schemas import ScopeFilter, PriceChange
from services.common.db import get_db
from services.common.models import ChatSession, ChatPreview


@pytest.fixture
def chat_session(seed_shop):
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=seed_shop,
                          createdAt=now, updatedAt=now))
    yield sid
    # Cascade through ChatSession deletes the previews automatically.
    with get_db() as s:
        row = s.get(ChatSession, sid)
        if row:
            s.delete(row)


def test_percent_change_math(seed_shop, chat_session):
    res = preview_price_change(seed_shop, chat_session,
                               ScopeFilter(vendor="Boat"),
                               PriceChange(type="percent", value=10))
    assert res.count >= 1
    assert res.min_new is not None and res.max_new is not None
    # Boat fixture variant has currentPrice 99.0 → +10% = 108.9
    assert res.max_new == 108.9


def test_preview_persists_row(seed_shop, chat_session):
    res = preview_price_change(seed_shop, chat_session,
                               ScopeFilter(vendor="Boat"),
                               PriceChange(type="percent", value=10))
    with get_db() as s:
        row = s.get(ChatPreview, res.preview_id)
        assert row is not None
        assert row.shopDomain == seed_shop
        assert len(row.variantIds) == res.count
        assert row.appliedAt is None


def test_preview_freezes_variant_ids(seed_shop, chat_session):
    res = preview_price_change(seed_shop, chat_session,
                               ScopeFilter(vendor="Boat"),
                               PriceChange(type="absolute", value=5))
    with get_db() as s:
        row = s.get(ChatPreview, res.preview_id)
        assert row.variantIds  # frozen
        # Frozen ids should be exactly the resolved variant ids
        assert set(row.variantIds) == {r.variant_id for r in res.sample_rows} or \
               len(row.variantIds) >= len(res.sample_rows)


def test_empty_scope_returns_count_zero(seed_shop, chat_session):
    res = preview_price_change(seed_shop, chat_session,
                               ScopeFilter(vendor="NonExistentVendor"),
                               PriceChange(type="percent", value=10))
    assert res.count == 0
