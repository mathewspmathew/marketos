"""services/chatbot_svc/tools/resolution_guard.py

Tracks which products a chatbot conversation has actually resolved via
resolve_product, and lets mutation tools refuse to run against a product_id
that was never really surfaced in this conversation — replacing a
prompt-only "call resolve_product first" instruction with a code-enforced
check. See docs/superpowers/specs/2026-07-18-code-enforced-product-resolution-design.md.
"""
from __future__ import annotations

from services.common.db import get_db
from services.common.models import ChatSession


def record_resolved_products(session_id: str, product_ids: list[str]) -> None:
    """Append product_ids (deduped) onto ChatSession.resolvedProductIds.
    Called by the resolve_product tool wrapper after every real resolution,
    including the zero-or-multiple-candidate cases."""
    if not product_ids:
        return
    with get_db() as s:
        session = s.get(ChatSession, session_id)
        if session is None:
            return
        existing = set(session.resolvedProductIds or [])
        existing.update(product_ids)
        session.resolvedProductIds = sorted(existing)


def ensure_product_resolved(session_id: str, product_id: str) -> None:
    """Raise RuntimeError if product_id is not in this session's
    resolvedProductIds. Called by every mutation tool before any other
    guard or write."""
    with get_db() as s:
        session = s.get(ChatSession, session_id)
        resolved = set(session.resolvedProductIds or []) if session is not None else set()

    if product_id not in resolved:
        raise RuntimeError(
            f"Product {product_id} hasn't been resolved in this conversation yet. "
            f"Call resolve_product first to get a valid product_id, then retry."
        )
