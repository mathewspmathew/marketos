from services.chatbot_svc.titling import is_refusal, REFUSAL_SENTENCE


def test_is_refusal_exact():
    assert is_refusal(REFUSAL_SENTENCE) is True


def test_is_refusal_trailing_whitespace_and_period():
    assert is_refusal("  I can only help with your store's products and pricing.  ") is True


def test_is_refusal_real_answer_is_false():
    assert is_refusal("Your cheapest variant is **$9.99**.") is False


def test_is_refusal_empty_is_false():
    assert is_refusal("") is False
    assert is_refusal(None) is False


import pytest
from services.chatbot_svc.titling import clean_title


def test_clean_title_strips_quotes_and_period():
    assert clean_title('"Nike price discount."') == "Nike price discount"


def test_clean_title_truncates_to_60_chars():
    long = "word " * 40
    assert len(clean_title(long)) <= 60


def test_clean_title_collapses_whitespace_and_newlines():
    assert clean_title("Nike\n  discount\tplan") == "Nike discount plan"


def test_clean_title_empty():
    assert clean_title("") == ""
    assert clean_title("   ") == ""


import uuid
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

from services.common.db import get_db
from services.common.models import ChatSession
from services.chatbot_svc.titling import maybe_set_title


def _make_session(shop):
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=shop, createdAt=now, updatedAt=now))
    return sid


def _title_of(sid):
    with get_db() as s:
        return s.get(ChatSession, sid).title


def _cleanup(sid):
    with get_db() as s:
        row = s.get(ChatSession, sid)
        if row:
            s.delete(row)


@pytest.mark.asyncio
async def test_maybe_set_title_sets_on_real_reply(seed_shop):
    sid = _make_session(seed_shop)
    try:
        with patch(
            "services.chatbot_svc.titling.generate_title",
            new=AsyncMock(return_value="Nike discount plan"),
        ):
            await maybe_set_title(sid, "make all Nike shoes 10% off", "Previewed 12 variants.")
        assert _title_of(sid) == "Nike discount plan"
    finally:
        _cleanup(sid)


@pytest.mark.asyncio
async def test_maybe_set_title_skips_refusal(seed_shop):
    sid = _make_session(seed_shop)
    try:
        with patch(
            "services.chatbot_svc.titling.generate_title",
            new=AsyncMock(return_value="Time question"),
        ) as gen:
            await maybe_set_title(sid, "what is the time now", REFUSAL_SENTENCE)
        assert _title_of(sid) is None
        gen.assert_not_called()
    finally:
        _cleanup(sid)


@pytest.mark.asyncio
async def test_maybe_set_title_noop_when_already_titled(seed_shop):
    sid = _make_session(seed_shop)
    with get_db() as s:
        s.get(ChatSession, sid).title = "Existing title"
    try:
        with patch(
            "services.chatbot_svc.titling.generate_title",
            new=AsyncMock(return_value="New title"),
        ) as gen:
            await maybe_set_title(sid, "another message", "A real answer.")
        assert _title_of(sid) == "Existing title"
        gen.assert_not_called()
    finally:
        _cleanup(sid)
