from __future__ import annotations
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete
from services.common.celery_app import app
from services.common.db import get_db
from services.common.models import ChatSession


@app.task(name="chatbot.prune_old_sessions")
def prune_old_sessions(days: int = 90) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with get_db() as s:
        result = s.execute(
            delete(ChatSession).where(ChatSession.updatedAt < cutoff)
        )
    return int(result.rowcount or 0)
