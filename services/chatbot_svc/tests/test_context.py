"""Unit tests for services.chatbot_svc.context."""
from services.chatbot_svc.context import count_tokens


def test_count_tokens_empty_string():
    assert count_tokens("") == 0


def test_count_tokens_short_text():
    # "hello world" = 11 chars; heuristic = 11 // 4 = 2, then max(1, ...) = 2
    assert count_tokens("hello world") == 2


def test_count_tokens_dict_payload():
    # Dicts must be serialised before counting.
    payload = {"text": "x" * 400}
    n = count_tokens(payload)
    # 400 chars + JSON overhead -> >=100 tokens, <=120
    assert 100 <= n <= 120


def test_count_tokens_minimum_is_one_for_nonempty():
    assert count_tokens("a") == 1
    assert count_tokens("ab") == 1


from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from services.chatbot_svc.context import row_to_model_message


def test_user_row_becomes_model_request():
    msg = row_to_model_message("user", {"text": "hello"})
    assert isinstance(msg, ModelRequest)
    assert isinstance(msg.parts[0], UserPromptPart)
    assert msg.parts[0].content == "hello"


def test_assistant_row_becomes_model_response():
    msg = row_to_model_message("assistant", {"text": "hi there"})
    assert isinstance(msg, ModelResponse)
    assert isinstance(msg.parts[0], TextPart)
    assert msg.parts[0].content == "hi there"


def test_assistant_ask_row_renders_question_text():
    # When the assistant raised ask_user, content has {"ask": {"question": "...", "options": [...]}}.
    msg = row_to_model_message(
        "assistant",
        {"ask": {"question": "Which vendor?", "options": ["Boat", "JBL"]}},
    )
    assert isinstance(msg, ModelResponse)
    assert "Which vendor?" in msg.parts[0].content


def test_tool_row_returns_none():
    msg = row_to_model_message("tool", {"tool_name": "apply", "tool_result": {"ok": True}})
    assert msg is None


def test_user_row_with_missing_text_returns_none():
    assert row_to_model_message("user", {}) is None


import uuid
from datetime import datetime, timezone, timedelta

import pytest

from services.common.db import get_db
from services.common.models import ChatMessage, ChatSession
from services.chatbot_svc.context import load_recent_messages


@pytest.fixture
def seed_session(seed_shop):
    sid = uuid.uuid4().hex
    base = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=seed_shop, createdAt=base, updatedAt=base))
        # 5 messages, increasing tokenCount, oldest first
        for i, tc in enumerate([10, 20, 30, 40, 50]):
            s.add(ChatMessage(
                id=f"{sid}-m{i}",
                sessionId=sid,
                role="user" if i % 2 == 0 else "assistant",
                content={"text": f"msg {i}"},
                tokenCount=tc,
                pinned=(i == 0),  # oldest is pinned
                createdAt=base + timedelta(seconds=i),
            ))
    yield sid
    with get_db() as s:
        s.query(ChatMessage).filter(ChatMessage.sessionId == sid).delete()
        s.query(ChatSession).filter(ChatSession.id == sid).delete()


def test_load_recent_respects_budget(seed_session):
    # Budget 70 -> would fit last two (50+40=90 > 70, so just 50), plus pinned (10).
    rows = load_recent_messages(seed_session, budget_tokens=70)
    ids = [r.id for r in rows]
    # Pinned msg 0 included, msg 4 (newest, 50 tokens) included.
    assert f"{seed_session}-m0" in ids
    assert f"{seed_session}-m4" in ids
    # Chronological order in result
    assert ids == sorted(ids, key=lambda x: int(x.rsplit("m", 1)[1]))


def test_load_recent_pinned_always_included(seed_session):
    # Budget 0 -> only pinned should come back.
    rows = load_recent_messages(seed_session, budget_tokens=0)
    assert [r.id for r in rows] == [f"{seed_session}-m0"]


def test_load_recent_large_budget_returns_all(seed_session):
    rows = load_recent_messages(seed_session, budget_tokens=10_000)
    assert len(rows) == 5


from services.chatbot_svc.context import build_context, DEFAULT_BUDGET_TOKENS


def test_build_context_returns_model_messages(seed_session):
    history = build_context(seed_session, budget_tokens=10_000)
    # 5 rows, all user/assistant -> all should convert.
    assert len(history) == 5
    # First is the oldest (pinned) -> user role (i=0 even) -> ModelRequest.
    assert isinstance(history[0], ModelRequest)


def test_build_context_default_budget_constant_exists():
    assert isinstance(DEFAULT_BUDGET_TOKENS, int)
    assert DEFAULT_BUDGET_TOKENS >= 4000
