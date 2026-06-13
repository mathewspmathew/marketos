"""Verify that agent.run is invoked with message_history derived from prior turns."""
import uuid
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from services.chatbot_svc.app import app
from services.common.db import get_db
from services.common.models import ChatMessage, ChatSession


@pytest.fixture
def session_with_prior(seed_shop):
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=seed_shop, createdAt=now, updatedAt=now))
        s.add(ChatMessage(
            id=f"{sid}-u1", sessionId=sid, role="user",
            content={"text": "my brand is Boat"}, tokenCount=5, pinned=False,
        ))
        s.add(ChatMessage(
            id=f"{sid}-a1", sessionId=sid, role="assistant",
            content={"text": "noted"}, tokenCount=2, pinned=False,
        ))
    yield seed_shop, sid
    with get_db() as s:
        s.query(ChatMessage).filter(ChatMessage.sessionId == sid).delete()
        s.query(ChatSession).filter(ChatSession.id == sid).delete()


def test_chat_passes_message_history(session_with_prior):
    shop, sid = session_with_prior
    client = TestClient(app)

    fake_result = type("R", (), {"output": "ok"})()
    with patch("services.chatbot_svc.app.agent.run", new=AsyncMock(return_value=fake_result)) as mock_run:
        client.post("/chat", json={
            "shop_domain": shop, "session_id": sid, "message": "what's my brand?",
        })
        assert mock_run.called
        kwargs = mock_run.call_args.kwargs
        assert "message_history" in kwargs
        history = kwargs["message_history"]
        # Two prior turns -> two ModelMessage entries.
        assert len(history) == 2
