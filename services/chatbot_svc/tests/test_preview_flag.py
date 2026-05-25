import pytest, uuid
from datetime import datetime, timezone
from services.chatbot_svc.tools.preview import preview_dynamic_pricing_toggle
from services.chatbot_svc.schemas import ScopeFilter
from services.common.db import get_db
from services.common.models import ChatSession, ChatPreview


@pytest.fixture
def chat_session(seed_shop):
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=seed_shop, createdAt=now, updatedAt=now))
    yield sid
    with get_db() as s:
        row = s.get(ChatSession, sid)
        if row:
            s.delete(row)


def test_toggle_preview_persists(seed_shop, chat_session):
    res = preview_dynamic_pricing_toggle(seed_shop, chat_session,
                                         ScopeFilter(vendor="Boat"), enabled=True)
    with get_db() as s:
        row = s.get(ChatPreview, res.preview_id)
        assert row is not None
        assert row.kind == "dynamic_pricing_toggle"
        assert row.change == {"enabled": True}
        assert row.variantIds  # product ids stored


def test_toggle_summary_mentions_dynamic_pricing(seed_shop, chat_session):
    res = preview_dynamic_pricing_toggle(seed_shop, chat_session,
                                         ScopeFilter(vendor="Boat"), enabled=True)
    assert res.count >= 1
    assert "dynamic pricing" in res.human_summary.lower()


def test_toggle_dedups_to_product_ids(seed_shop, chat_session):
    res = preview_dynamic_pricing_toggle(seed_shop, chat_session,
                                         ScopeFilter(vendor="Boat"), enabled=False)
    with get_db() as s:
        row = s.get(ChatPreview, res.preview_id)
        # length should equal distinct product count, which is <= variant count
        assert len(row.variantIds) == len(set(row.variantIds))
