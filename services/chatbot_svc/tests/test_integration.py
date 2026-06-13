import uuid
from datetime import datetime, timezone
import pytest

from services.common.db import get_db
from services.common.models import ChatSession
from services.chatbot_svc.tools.preview import (
    preview_price_change, preview_dynamic_pricing_toggle,
)
from services.chatbot_svc.tools.stats import get_stats, StatsMetric
from services.chatbot_svc.schemas import ScopeFilter, PriceChange


@pytest.fixture
def session_row(seed_shop):
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=seed_shop, createdAt=now, updatedAt=now))
    yield sid
    with get_db() as s:
        row = s.get(ChatSession, sid)
        if row:
            s.delete(row)


@pytest.mark.asyncio
async def test_full_price_change_flow(seed_shop, session_row):
    """(b) Price change: the agent previews and STOPS — applying is the card's
    job, so there is no Python apply step. Assert a pending preview persists."""
    prev = preview_price_change(
        seed_shop, session_row,
        ScopeFilter(vendor="Boat"),
        PriceChange(type="percent", value=10),
    )
    assert prev.count >= 1
    assert prev.kind == "price_change"
    # A pending (unapplied) ChatPreview row exists for the card to apply.
    with get_db() as s:
        from services.common.models import ChatPreview
        row = s.get(ChatPreview, prev.preview_id)
        assert row is not None
        assert row.appliedAt is None


@pytest.mark.asyncio
async def test_full_dynamic_pricing_toggle_flow(seed_shop, session_row):
    """(a) Toggle: the agent previews and STOPS — applying is the card's job, so
    there is no Python apply step. Assert a pending preview persists for the card."""
    prev = preview_dynamic_pricing_toggle(
        seed_shop, session_row,
        ScopeFilter(vendor="Boat"),
        enabled=True,
    )
    assert prev.count >= 1
    assert prev.kind == "dynamic_pricing_toggle"
    # A pending (unapplied) ChatPreview row exists for the card to apply.
    with get_db() as s:
        from services.common.models import ChatPreview
        row = s.get(ChatPreview, prev.preview_id)
        assert row is not None
        assert row.appliedAt is None


def test_intent_c_stats_no_confirm_required(seed_shop):
    """(c) Stats: get_stats answers directly, no preview gate."""
    out = get_stats(seed_shop, StatsMetric.match_coverage)
    assert "matched" in out and "total" in out
    assert isinstance(out["total"], int)


def test_second_turn_includes_first_turn_history(seed_shop):
    """After turn 1 persists, turn 2's agent.run must receive turn 1 in message_history."""
    import json
    from unittest.mock import patch, AsyncMock
    from fastapi.testclient import TestClient
    from pydantic_ai.messages import ModelRequest
    from services.chatbot_svc.app import app

    client = TestClient(app)
    fake = type("R", (), {"output": "ack"})()

    sid = None
    try:
        with patch("services.chatbot_svc.app.agent.run", new=AsyncMock(return_value=fake)) as mock_run:
            # Turn 1: creates session.
            r1 = client.post("/chat", json={"shop_domain": seed_shop, "message": "I sell speakers"})
            for line in r1.iter_lines():
                if line.startswith("data: ") and "session_id" in line:
                    sid = json.loads(line[6:])["session_id"]
                    break
            assert sid

            # Turn 2: should replay turn 1.
            client.post("/chat", json={"shop_domain": seed_shop, "session_id": sid, "message": "what do I sell?"})

            # Second call's history should include the user message from turn 1.
            second_call_history = mock_run.call_args_list[1].kwargs["message_history"]
            assert len(second_call_history) >= 2  # user + assistant from turn 1
            # First entry is turn 1's user message.
            assert isinstance(second_call_history[0], ModelRequest)
            assert "speakers" in second_call_history[0].parts[0].content
    finally:
        # Clean up chat rows so the seed_shop fixture can tear down (RESTRICT FK).
        if sid:
            with get_db() as s:
                from services.common.models import ChatMessage as _CM, ChatPreview as _CP
                s.query(_CM).filter(_CM.sessionId == sid).delete()
                s.query(_CP).filter(_CP.sessionId == sid).delete()
                row = s.get(ChatSession, sid)
                if row:
                    s.delete(row)
