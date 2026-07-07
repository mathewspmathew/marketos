import pytest, uuid
from datetime import datetime, timezone
from services.chatbot_svc.tools.panel import open_dynamic_pricing_panel
from services.common.db import get_db
from services.common.models import ChatSession, ChatPreview, ShopifyProduct


def _pid(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(
            ShopifyProduct.shopDomain == shop).scalar()


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
    pid = _pid(seed_shop)
    res = open_dynamic_pricing_panel(seed_shop, chat_session, pid)
    with get_db() as s:
        row = s.get(ChatPreview, res.preview_id)
        assert row is not None
        assert row.kind == "dynamic_pricing_toggle"
        assert row.change["panel"] is True
        assert row.change["cardState"] == "FRESH"
        assert row.change["allowedActions"] == ["enable"]
        assert row.change["rescrape"] is False
        assert row.change["numResults"] == 10
        assert row.variantIds == [pid]  # product ids stored


def test_toggle_summary_mentions_dynamic_pricing(seed_shop, chat_session):
    pid = _pid(seed_shop)
    res = open_dynamic_pricing_panel(seed_shop, chat_session, pid)
    assert res.kind == "dynamic_pricing_toggle"
    assert res.card_state == "FRESH"
    assert "setup" in res.human_summary.lower()


def test_toggle_dedups_to_product_ids(seed_shop, chat_session):
    pid = _pid(seed_shop)
    res = open_dynamic_pricing_panel(seed_shop, chat_session, pid)
    with get_db() as s:
        row = s.get(ChatPreview, res.preview_id)
        # panel freezes exactly one product id, with no duplicates
        assert len(row.variantIds) == len(set(row.variantIds))
        assert row.variantIds == [pid]
