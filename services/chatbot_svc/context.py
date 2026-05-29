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
