"""
services/common/groq_client.py

Groq client with automatic fallback to a backup API key on auth failure.

Both GROQ_API_KEY (primary) and GROQ_API_KEY_BACKUP rotate independently; if
the primary returns 401/403, we transparently switch to the backup for the
rest of this process's lifetime. The switch is logged once.

Call sites use the same `.chat.completions.create(...)` API as the raw Groq
SDK — `_FallbackGroq` proxies to whichever client is currently active.
"""
from __future__ import annotations

import os
import threading
from typing import Any

import structlog
from groq import AuthenticationError, Groq, PermissionDeniedError

logger = structlog.get_logger(__name__)


class _FallbackGroq:
    """Proxy that retries `.chat.completions.create` on the backup key after
    an auth failure on the primary. Only the first switch is logged."""

    def __init__(self, primary_env: str = "GROQ_API_KEY", backup_env: str = "GROQ_API_KEY_BACKUP"):
        self._primary_key = os.getenv(primary_env, "not-set")
        self._backup_key  = os.getenv(backup_env)
        self._client      = Groq(api_key=self._primary_key)
        self._using_backup = False
        self._lock = threading.Lock()
        # Expose the same attribute surface as the SDK.
        self.chat = _Chat(self)

    def _switch_to_backup(self) -> bool:
        """Swap to the backup key. Returns True if a switch actually happened."""
        with self._lock:
            if self._using_backup or not self._backup_key:
                return False
            self._client = Groq(api_key=self._backup_key)
            self._using_backup = True
            logger.warning("groq_backup_key_activated")
            return True


class _Chat:
    def __init__(self, parent: _FallbackGroq):
        self._parent = parent
        self.completions = _Completions(parent)


class _Completions:
    def __init__(self, parent: _FallbackGroq):
        self._parent = parent

    def create(self, **kwargs: Any):
        try:
            return self._parent._client.chat.completions.create(**kwargs)
        except (AuthenticationError, PermissionDeniedError):
            if self._parent._switch_to_backup():
                return self._parent._client.chat.completions.create(**kwargs)
            raise


def make_groq_client(primary_env: str = "GROQ_API_KEY",
                     backup_env: str = "GROQ_API_KEY_BACKUP") -> _FallbackGroq:
    return _FallbackGroq(primary_env=primary_env, backup_env=backup_env)
