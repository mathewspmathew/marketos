"""Verify new chatbot context columns are mapped on the SQLAlchemy models."""
from services.common.models import ChatMessage, ChatSession


def test_chat_message_has_token_count_and_pinned():
    cols = {c.name for c in ChatMessage.__table__.columns}
    assert "tokenCount" in cols
    assert "pinned" in cols


def test_chat_session_has_running_summary():
    cols = {c.name for c in ChatSession.__table__.columns}
    assert "runningSummary" in cols
