"""Context builder for the chatbot agent.

Builds the `message_history` argument for `agent.run` from prior
`ChatMessage` rows, applying a token budget and including pinned
messages unconditionally.
"""
from __future__ import annotations

import json
from typing import Any


def count_tokens(payload: Any) -> int:
    """Approximate token count using a 4-chars-per-token heuristic.

    JSON-serialises non-string inputs first. Returns 0 for empty input
    and at least 1 for any non-empty input.
    """
    if payload is None or payload == "" or payload == {}:
        return 0
    text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    if not text:
        return 0
    return max(1, len(text) // 4)


from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)


def row_to_model_message(role: str, content: dict) -> ModelMessage | None:
    """Convert a single `ChatMessage` row to a pydantic-ai `ModelMessage`.

    Returns None when the row should be skipped (tool rows, malformed bodies).
    """
    if role == "user":
        text = content.get("text") if isinstance(content, dict) else None
        if not text:
            return None
        return ModelRequest(parts=[UserPromptPart(content=text)])

    if role == "assistant":
        if not isinstance(content, dict):
            return None
        if "text" in content and content["text"]:
            return ModelResponse(parts=[TextPart(content=content["text"])])
        if "ask" in content and isinstance(content["ask"], dict):
            ask = content["ask"]
            q = ask.get("question", "")
            opts = ask.get("options") or []
            opts_str = f" Options: {', '.join(opts)}." if opts else ""
            return ModelResponse(parts=[TextPart(content=f"[asked user] {q}{opts_str}")])
        return None

    # Tool rows are not replayed in v1 — see plan doc for rationale.
    return None


from services.common.db import get_db
from services.common.models import ChatMessage


def load_recent_messages(
    session_id: str,
    budget_tokens: int,
) -> list[ChatMessage]:
    """Return chat messages for a session, oldest→newest, budget-bounded.

    Walks the session's messages newest→oldest, accumulating those that
    fit in `budget_tokens`. Pinned messages are included unconditionally
    (their cost does not count against the budget). The result is returned
    in chronological order so it can be replayed directly to the LLM.
    """
    with get_db() as s:
        # Pinned set, returned regardless of budget.
        pinned_rows = (
            s.query(ChatMessage)
            .filter(ChatMessage.sessionId == session_id, ChatMessage.pinned.is_(True))
            .all()
        )
        # Unpinned, newest first, for budget walk.
        unpinned_rows = (
            s.query(ChatMessage)
            .filter(ChatMessage.sessionId == session_id, ChatMessage.pinned.is_(False))
            .order_by(ChatMessage.createdAt.desc())
            .all()
        )

        selected: list[ChatMessage] = []
        used = 0
        for row in unpinned_rows:
            cost = row.tokenCount if row.tokenCount is not None else count_tokens(row.content)
            if used + cost > budget_tokens:
                break
            selected.append(row)
            used += cost

        combined = pinned_rows + selected
        combined.sort(key=lambda r: r.createdAt)
        # Detach so callers can access attributes after the session closes.
        for row in combined:
            s.expunge(row)
        return combined


DEFAULT_BUDGET_TOKENS = 12_000


def build_context(
    session_id: str,
    *,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    summary: str | None = None,  # Reserved: running summary, unused in v1.
) -> list[ModelMessage]:
    """Build the `message_history` argument for `agent.run`.

    Loads recent messages within `budget_tokens`, including pinned
    messages unconditionally, and converts each to a `ModelMessage`.

    The `summary` parameter is accepted but ignored in v1. When the
    running-summary feature lands, it will be prepended as a synthetic
    `ModelRequest` system note.
    """
    _ = summary  # placeholder until summary feature lands
    rows = load_recent_messages(session_id, budget_tokens=budget_tokens)
    messages: list[ModelMessage] = []
    for row in rows:
        msg = row_to_model_message(row.role, row.content)
        if msg is not None:
            messages.append(msg)
    return messages
