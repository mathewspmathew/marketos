import uuid
from datetime import datetime, timezone

import pytest

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, CompetitorCandidate, ChatPreview, ChatSession,
)
from services.chatbot_svc.schemas import ScopeFilter
from services.chatbot_svc.tools.preview import preview_dynamic_pricing_toggle


def _pid(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(
            ShopifyProduct.shopDomain == shop).scalar()


def _scope(pid):
    return ScopeFilter(product_ids=[pid])


def _stored_summary(preview_id):
    with get_db() as s:
        return s.get(ChatPreview, preview_id).summary


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


def test_fresh_preview_has_fresh_state(seed_shop, chat_session):
    pid = _pid(seed_shop)
    res = preview_dynamic_pricing_toggle(seed_shop, chat_session, _scope(pid), True)
    summary = _stored_summary(res.preview_id)
    assert summary["enableContext"]["state"] == "FRESH"
    assert ("first time" in res.human_summary.lower()
            or "set up" in res.human_summary.lower())


def test_paused_preview_reports_existing_data(seed_shop, chat_session):
    pid = _pid(seed_shop)
    candidate_id = str(uuid.uuid4())
    with get_db() as s:
        s.add(CompetitorCandidate(
            id=candidate_id, shopDomain=seed_shop, shopifyProductId=pid,
            url="https://x.test/p", domain="x.test", source="serper_search",
            status="SCRAPED"))
    try:
        res = preview_dynamic_pricing_toggle(seed_shop, chat_session, _scope(pid), True)
        summary = _stored_summary(res.preview_id)
        assert summary["enableContext"]["state"] == "PAUSED_WITH_DATA"
        assert summary["enableContext"]["competitors_found"] == 1
        assert "1" in res.human_summary
    finally:
        with get_db() as s:
            s.query(CompetitorCandidate).filter(
                CompetitorCandidate.id == candidate_id).delete(synchronize_session=False)
