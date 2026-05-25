from __future__ import annotations
from dataclasses import dataclass


@dataclass
class AskUserRequested(Exception):
    """Sentinel raised by ask_user; caught by the chat endpoint to emit SSE."""
    question: str
    options: list[str] | None = None

    def __str__(self) -> str:
        return f"AskUserRequested({self.question!r})"


def ask_user(question: str, options: list[str] | None = None) -> str:
    """Pause the agent loop and surface a clarification to the user."""
    raise AskUserRequested(question=question, options=options)
