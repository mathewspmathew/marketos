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
