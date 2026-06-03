# services/chatbot_svc/tests/test_sessions.py
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from services.common.db import get_db
from services.common.models import ChatSession, ChatMessage
from services.chatbot_svc import sessions as S


def _mk_session(shop, title=None, ago_seconds=0):
    sid = uuid.uuid4().hex
    ts = datetime.now(timezone.utc) - timedelta(seconds=ago_seconds)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=shop, title=title, createdAt=ts, updatedAt=ts))
    return sid


def _mk_message(sid, role, text):
    with get_db() as s:
        s.add(ChatMessage(id=uuid.uuid4().hex, sessionId=sid, role=role,
                          content={"text": text}, createdAt=datetime.now(timezone.utc)))


def _drop(*sids):
    with get_db() as s:
        for sid in sids:
            row = s.get(ChatSession, sid)
            if row:
                s.delete(row)


def test_list_sessions_orders_newest_first_with_counts(seed_shop):
    old = _mk_session(seed_shop, title="Older", ago_seconds=100)
    new = _mk_session(seed_shop, title="Newer", ago_seconds=0)
    _mk_message(new, "user", "hi")
    _mk_message(new, "assistant", "hello")
    try:
        rows = S.list_sessions(seed_shop)
        ids = [r["id"] for r in rows]
        assert ids.index(new) < ids.index(old)  # newest first
        new_row = next(r for r in rows if r["id"] == new)
        assert new_row["title"] == "Newer"
        assert new_row["message_count"] == 2
    finally:
        _drop(old, new)


def test_list_sessions_scoped_to_shop(seed_shop, seed_other_shop):
    mine = _mk_session(seed_shop, title="Mine")
    theirs = _mk_session(seed_other_shop, title="Theirs")
    try:
        ids = [r["id"] for r in S.list_sessions(seed_shop)]
        assert mine in ids
        assert theirs not in ids
    finally:
        _drop(mine, theirs)


def test_delete_session_removes_owned(seed_shop):
    sid = _mk_session(seed_shop, title="ToDelete")
    _mk_message(sid, "user", "hi")
    assert S.delete_session(seed_shop, sid) is True
    with get_db() as s:
        assert s.get(ChatSession, sid) is None
        assert s.query(ChatMessage).filter(ChatMessage.sessionId == sid).count() == 0


def test_delete_session_refuses_other_shop(seed_shop, seed_other_shop):
    theirs = _mk_session(seed_other_shop, title="Theirs")
    try:
        assert S.delete_session(seed_shop, theirs) is False
        with get_db() as s:
            assert s.get(ChatSession, theirs) is not None
    finally:
        _drop(theirs)


def test_delete_all_sessions_clears_only_this_shop(seed_shop, seed_other_shop):
    a = _mk_session(seed_shop, title="A")
    b = _mk_session(seed_shop, title="B")
    theirs = _mk_session(seed_other_shop, title="Theirs")
    try:
        n = S.delete_all_sessions(seed_shop)
        assert n == 2
        assert S.list_sessions(seed_shop) == []
        with get_db() as s:
            assert s.get(ChatSession, theirs) is not None
    finally:
        _drop(a, b, theirs)


def test_get_turns_returns_chronological_panel_shapes(seed_shop):
    sid = _mk_session(seed_shop, title="Chat")
    with get_db() as s:
        base = datetime.now(timezone.utc)
        s.add(ChatMessage(id=uuid.uuid4().hex, sessionId=sid, role="user",
                          content={"text": "hello"}, createdAt=base))
        s.add(ChatMessage(id=uuid.uuid4().hex, sessionId=sid, role="assistant",
                          content={"text": "hi there"},
                          createdAt=base + timedelta(seconds=1)))
        s.add(ChatMessage(id=uuid.uuid4().hex, sessionId=sid, role="assistant",
                          content={"ask": {"question": "Which vendor?", "options": ["Nike"]}},
                          createdAt=base + timedelta(seconds=2)))
        s.add(ChatMessage(id=uuid.uuid4().hex, sessionId=sid, role="tool",
                          content={"tool_name": "apply"},
                          createdAt=base + timedelta(seconds=3)))
    try:
        turns = S.get_turns(seed_shop, sid)
        assert turns == [
            {"role": "user", "text": "hello"},
            {"role": "assistant", "text": "hi there"},
            {"role": "assistant", "ask": {"question": "Which vendor?", "options": ["Nike"]}},
        ]  # tool row is skipped
    finally:
        _drop(sid)


def test_get_turns_refuses_other_shop(seed_shop, seed_other_shop):
    theirs = _mk_session(seed_other_shop, title="Theirs")
    try:
        assert S.get_turns(seed_shop, theirs) is None
    finally:
        _drop(theirs)
