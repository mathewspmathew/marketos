"""Auto-titling for chat sessions.

After the first *non-refused* exchange in a session, an LLM generates a
short (3-5 word) title, matching how ChatGPT/Claude title conversations.
Refused (off-topic) and untitled-yet sessions show "New chat" in the UI.
"""
from __future__ import annotations

import os

from pydantic_ai import Agent

REFUSAL_SENTENCE = "I can only help with your store's products and pricing."

_MODEL = os.environ.get("CHATBOT_MODEL", "groq:llama-3.3-70b-versatile")

_TITLE_PROMPT = (
    "You write a very short title for a chat conversation in an e-commerce "
    "pricing dashboard. Given the user's first message, reply with a 3-5 word "
    "title summarizing the topic. Reply with ONLY the title: no quotes, no "
    "punctuation, no preamble."
)

# A tool-less agent dedicated to titling, separate from the main chat agent.
_title_agent: Agent[None, str] = Agent(_MODEL, system_prompt=_TITLE_PROMPT)


def is_refusal(text: str | None) -> bool:
    """True when `text` is the store-only refusal reply (ignoring surrounding
    whitespace and a trailing period)."""
    if not text:
        return False
    normalized = text.strip().rstrip(".").strip()
    return normalized == REFUSAL_SENTENCE.rstrip(".")


def clean_title(raw: str) -> str:
    """Normalize an LLM title: collapse whitespace, strip wrapping quotes and a
    trailing period, and cap length at 60 chars."""
    if not raw:
        return ""
    text = " ".join(raw.split())          # collapse all whitespace/newlines
    text = text.strip().strip('"').strip("'").strip()
    text = text.rstrip(".").strip()
    return text[:60]


async def generate_title(first_message: str) -> str:
    """Call the title agent and return a cleaned 3-5 word title (or '')."""
    result = await _title_agent.run(first_message)
    output = result.output if hasattr(result, "output") else getattr(result, "data", "")
    return clean_title(output if isinstance(output, str) else "")
