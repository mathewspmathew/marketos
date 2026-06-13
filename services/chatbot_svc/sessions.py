# services/chatbot_svc/sessions.py
"""Shop-scoped chat-session queries for the chatbot service.

Every function takes `shop` as the first argument and filters on it, so a
merchant can never list, read, or delete another shop's chats. Message and
preview rows are removed by the DB-level ON DELETE CASCADE on their sessionId
foreign keys when a ChatSession is deleted.
"""
from __future__ import annotations

from sqlalchemy import func

from services.common.db import get_db
from services.common.models import ChatMessage, ChatSession


def list_sessions(shop: str) -> list[dict]:
    """Sessions for `shop`, newest `updatedAt` first, with message counts."""
    with get_db() as s:
        rows = (
            s.query(ChatSession, func.count(ChatMessage.id).label("message_count"))
            .outerjoin(ChatMessage, ChatMessage.sessionId == ChatSession.id)
            .filter(ChatSession.shopDomain == shop)
            .group_by(ChatSession.id)
            .order_by(ChatSession.updatedAt.desc())
            .all()
        )
        return [
            {
                "id": sess.id,
                "title": sess.title,
                "updated_at": sess.updatedAt.isoformat() if sess.updatedAt else None,
                "message_count": int(count),
            }
            for sess, count in rows
        ]


def delete_session(shop: str, session_id: str) -> bool:
    """Delete one session owned by `shop`. Returns True if a row was deleted.

    Returns False (0 rows deleted) for both "session not found" and "session
    belongs to a different shop". This is intentional: the calling route maps
    both cases to 404, so existence is never leaked across shops.
    """
    with get_db() as s:
        deleted = (
            s.query(ChatSession)
            .filter(ChatSession.id == session_id, ChatSession.shopDomain == shop)
            .delete(synchronize_session=False)
        )
        return deleted > 0


def delete_all_sessions(shop: str) -> int:
    """Delete every session owned by `shop`. Returns the number deleted."""
    with get_db() as s:
        return (
            s.query(ChatSession)
            .filter(ChatSession.shopDomain == shop)
            .delete(synchronize_session=False)
        )


def get_turns(shop: str, session_id: str) -> list[dict] | None:
    """Return a session's messages as front-end turn objects, oldest first.

    Returns None if the session does not exist or is not owned by `shop`.
    Tool rows and malformed bodies are skipped. Shapes match what the SSE
    stream emits: {role:"user", text}, {role:"assistant", text},
    {role:"assistant", ask:{question, options}}.
    """
    with get_db() as s:
        sess = s.get(ChatSession, session_id)
        if sess is None or sess.shopDomain != shop:
            return None
        rows = (
            s.query(ChatMessage)
            .filter(ChatMessage.sessionId == session_id)
            .order_by(ChatMessage.createdAt)
            .all()
        )
        turns: list[dict] = []
        for row in rows:
            # JSONB content is always a dict in practice; fallback guards against
            # any unexpected deserialization edge-cases (e.g. None or raw string).
            content = row.content if isinstance(row.content, dict) else {}
            if row.role == "user" and content.get("text"):
                turns.append({"role": "user", "text": content["text"]})
            elif row.role == "assistant" and content.get("text"):
                turns.append({"role": "assistant", "text": content["text"]})
            elif row.role == "assistant" and isinstance(content.get("ask"), dict):
                turns.append({"role": "assistant", "ask": content["ask"]})
            # tool rows and anything else are skipped
        return turns
