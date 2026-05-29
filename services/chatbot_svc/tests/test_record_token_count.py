"""Verify _record persists tokenCount on insert."""
import uuid
from datetime import datetime, timezone

from services.chatbot_svc.app import _record
from services.common.db import get_db
from services.common.models import ChatMessage, ChatSession


def test_record_sets_token_count(seed_shop):
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=seed_shop, createdAt=now, updatedAt=now))

    _record(sid, "user", {"text": "hello world this is a test"})

    with get_db() as s:
        msg = s.query(ChatMessage).filter(ChatMessage.sessionId == sid).one()
        assert msg.tokenCount is not None
        assert msg.tokenCount >= 1

    # Cleanup
    with get_db() as s:
        s.query(ChatMessage).filter(ChatMessage.sessionId == sid).delete()
        s.query(ChatSession).filter(ChatSession.id == sid).delete()
