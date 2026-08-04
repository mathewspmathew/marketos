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

    # NullPool: open a connection on checkout, close it for real on session.close().
    # QueuePool with pool_size=1 held one connection open per process for its
    # entire lifetime — with a dozen mostly-idle worker processes and no pooler
    # in front, idle connections alone exhausted the shared Postgres instance's
    # connection ceiling. A long-lived pooled connection is also the wrong shape
    # once behind PgBouncer's transaction-pooling mode: psycopg3 auto-prepares
    # statements on a connection it reuses repeatedly, and a prepared statement
    # can go stale if PgBouncer swaps the backend under it between transactions.
    # NullPool never reuses a connection object, so that failure mode can't
    # happen, and PgBouncer (not this pool) now absorbs the handshake cost that
    # used to justify holding a connection open.
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
