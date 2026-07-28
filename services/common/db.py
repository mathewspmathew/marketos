"""
services/common/db.py

SQLAlchemy sync engine — one connection pool per OS process.
Celery prefork workers each get their own pool; sessions are checked out
per task and returned to the pool immediately after.
No asyncio, no event loops, no Prisma.
"""
import os
from contextlib import contextmanager

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool

load_dotenv()

_engine = None
_SessionLocal = None


def _build_engine():
    url = os.environ["DATABASE_URL"]
    # Normalise any postgresql:// or postgres:// URL to the psycopg3 dialect.
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg" + url[len(prefix) - 3:]
            break

    if os.environ.get("DB_POOL_MODE") == "queue":
        # For a long-lived single-process service (e.g. api_gateway's uvicorn
        # process) fielding frequent interactive requests, NullPool's
        # per-request handshake (~470ms measured against Aiven) dwarfs the
        # actual query cost (~40ms). A small persistent pool amortizes that
        # handshake across requests. Capped very small (max 3 held
        # connections) because the shared Postgres instance's max_connections
        # (20) has limited headroom from other services/dev sessions already
        # holding connections — checked live via pg_stat_activity before
        # picking this size. Revisit upward only after confirming headroom.
        return create_engine(
            url,
            pool_size=2, max_overflow=1,
            pool_recycle=1800,
            pool_pre_ping=True,
        )

    # NullPool: open a connection on checkout, close it for real on session.close().
    # QueuePool with pool_size=1 held one connection open per process for its
    # entire lifetime — with a dozen mostly-idle worker processes and no pooler
    # in front, idle connections alone exhausted the shared Postgres instance's
    # connection ceiling. NullPool trades a ~20-50ms handshake per task (noise
    # next to the multi-second Groq/Firecrawl/Serper calls each task makes) for
    # not holding a slot open while idle. Revisit with a pooler (PgBouncer/Aiven
    # managed pool) if task throughput grows enough for that handshake to matter.
    return create_engine(url, poolclass=NullPool)


def _get_session_factory():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _build_engine()
        _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
    return _SessionLocal

# we use contextmanager to ensure that the session is properly closed after use, even if an error occurs.
# so we can use with get_db() as session
@contextmanager
def get_db() -> Session:
    """Yield a SQLAlchemy session. Commits on success, rolls back on error."""
    session = _get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
