"""Auto-titling for chat sessions.

After the first *non-refused* exchange in a session, an LLM generates a
short (3-5 word) title, matching how ChatGPT/Claude title conversations.
Refused (off-topic) and untitled-yet sessions show "New chat" in the UI.
"""
from __future__ import annotations

REFUSAL_SENTENCE = "I can only help with your store's products and pricing."


def is_refusal(text: str | None) -> bool:
    """True when `text` is the store-only refusal reply (ignoring surrounding
    whitespace and a trailing period)."""
    if not text:
        return False
    normalized = text.strip().rstrip(".").strip()
    return normalized == REFUSAL_SENTENCE.rstrip(".")
