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
    return create_engine(
        url,
        pool_size=5,        # connections kept open per worker process
        max_overflow=10,    # extra connections allowed under burst load
        pool_pre_ping=True, # discard stale connections before use
        pool_recycle=1800,  # recycle connections every 30 min (avoids TCP timeouts)
        pool_timeout=30,    # raise after 30 s if no connection is available
    )


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
