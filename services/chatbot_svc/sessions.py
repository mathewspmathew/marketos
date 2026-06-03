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
            s.query(ChatSession, func.count(ChatMessage.id))
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
    """Delete one session owned by `shop`. Returns True if a row was deleted."""
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
