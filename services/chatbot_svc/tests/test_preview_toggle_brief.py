# services/chatbot_svc/tests/test_preview_toggle_brief.py
import os
os.environ.setdefault("GROQ_API_KEY", "test")

import uuid
from datetime import datetime, timezone

from services.common.db import get_db
from services.common.models import ChatSession, ChatPreview
from services.chatbot_svc.tools.preview import preview_dynamic_pricing_toggle
from services.chatbot_svc.schemas import ScopeFilter


def _session(shop):
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=shop, createdAt=now, updatedAt=now))
    return sid


def _cleanup(sid):
    with get_db() as s:
        s.query(ChatPreview).filter(ChatPreview.sessionId == sid).delete()
        row = s.get(ChatSession, sid)
        if row:
            s.delete(row)


def test_enable_preview_freezes_resolved_settings(seed_shop):
    sid = _session(seed_shop)
    try:
        res = preview_dynamic_pricing_toggle(seed_shop, sid, ScopeFilter(vendor="Boat"), enabled=True)
        with get_db() as s:
            row = s.get(ChatPreview, res.preview_id)
            assert row.change["enabled"] is True
            assert row.change["rescrape"] is False
            assert row.change["numResults"] == 10
            assert row.change["listingExpansionCap"] == 5
            assert row.change["query"] == "Boat Speaker White"
    finally:
        _cleanup(sid)


def test_disable_preview_carries_delete_counts(seed_shop):
    sid = _session(seed_shop)
    try:
        res = preview_dynamic_pricing_toggle(seed_shop, sid, ScopeFilter(vendor="Boat"), enabled=False)
        with get_db() as s:
            row = s.get(ChatPreview, res.preview_id)
            counts = row.summary["deleteCounts"]
            assert counts["discovered_links"] == 0
            assert counts["competitor_products"] == 0
            assert counts["price_stats_variants"] == 1
            assert row.change == {"enabled": False}
    finally:
        _cleanup(sid)
