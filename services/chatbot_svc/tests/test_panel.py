# services/chatbot_svc/tests/test_panel.py
import os
os.environ.setdefault("GROQ_API_KEY", "test")

import uuid
from datetime import datetime, timezone

import pytest

from services.common.db import get_db
from services.common.models import (
    ChatPreview, ChatSession, CompetitorCandidate, ShopifyProduct,
)
from services.chatbot_svc.tools.panel import open_dynamic_pricing_panel


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


def _product_id(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(
            ShopifyProduct.shopDomain == shop).scalar()


def _add_candidate(shop, pid):
    with get_db() as s:
        s.add(CompetitorCandidate(
            id=str(uuid.uuid4()), shopDomain=shop, shopifyProductId=pid,
            url="https://x.test/p", domain="x.test", source="serper_search",
            status="SCRAPED",
        ))


def _clear_candidates(shop, pid):
    with get_db() as s:
        s.query(CompetitorCandidate).filter(
            CompetitorCandidate.shopDomain == shop,
            CompetitorCandidate.shopifyProductId == pid,
        ).delete()


def test_fresh_product_gets_editable_enable_card(seed_shop):
    sid = _session(seed_shop)
    pid = _product_id(seed_shop)
    try:
        res = open_dynamic_pricing_panel(seed_shop, sid, pid)
        assert res.card_state == "FRESH"
        assert res.kind == "dynamic_pricing_toggle"
        assert res.product_id == pid
        with get_db() as s:
            row = s.get(ChatPreview, res.preview_id)
            assert row.kind == "dynamic_pricing_toggle"
            assert row.variantIds == [pid]
            assert row.change["panel"] is True
            assert row.change["cardState"] == "FRESH"
            assert row.change["allowedActions"] == ["enable"]
            assert row.change["numResults"] == 10
            assert row.change["listingExpansionCap"] == 5
            assert row.change["query"] == "Boat Speaker White"
            assert row.summary["product"]["title"] == "Boat Speaker White"
            assert row.summary["deleteCounts"] is None
    finally:
        _cleanup(sid)


def test_active_product_gets_readonly_pause_delete_card(seed_shop):
    sid = _session(seed_shop)
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.get(ShopifyProduct, pid).dynamicPricingEnabled = True
    try:
        res = open_dynamic_pricing_panel(seed_shop, sid, pid)
        assert res.card_state == "ACTIVE"
        with get_db() as s:
            row = s.get(ChatPreview, res.preview_id)
            assert row.change["cardState"] == "ACTIVE"
            assert row.change["allowedActions"] == ["pause", "delete"]
            assert "numResults" not in row.change  # no form on read-only cards
            assert row.summary["deleteCounts"]["price_stats_variants"] == 1
    finally:
        _cleanup(sid)


def test_paused_product_gets_readonly_resume_delete_card(seed_shop):
    sid = _session(seed_shop)
    pid = _product_id(seed_shop)
    _add_candidate(seed_shop, pid)
    try:
        res = open_dynamic_pricing_panel(seed_shop, sid, pid)
        assert res.card_state == "PAUSED"
        with get_db() as s:
            row = s.get(ChatPreview, res.preview_id)
            assert row.change["allowedActions"] == ["resume", "delete"]
            assert row.summary["enableContext"]["competitors_found"] == 1
            assert row.summary["deleteCounts"]["discovered_links"] == 1
    finally:
        _clear_candidates(seed_shop, pid)
        _cleanup(sid)


def test_unknown_product_raises(seed_shop):
    sid = _session(seed_shop)
    try:
        with pytest.raises(RuntimeError, match="resolve_product"):
            open_dynamic_pricing_panel(seed_shop, sid, "gid://does-not-exist")
    finally:
        _cleanup(sid)


def test_cross_shop_product_raises(seed_shop, seed_other_shop):
    sid = _session(seed_shop)
    try:
        with pytest.raises(RuntimeError, match="resolve_product"):
            open_dynamic_pricing_panel(seed_shop, sid, "other-shop-product")
    finally:
        _cleanup(sid)
