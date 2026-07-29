"""services/chatbot_svc/model_cooldown.py

In-process cooldown tracker for the chatbot's Groq model fallback chain.

pydantic-ai's FallbackModel has no memory of past failures -- it retries the
primary model from scratch on every agent.run() call, even seconds after that
model just 429'd (see pydantic_ai.models.fallback.FallbackModel.request:
`for model in self.models:` always starts at index 0). Under sustained load
this hammers an already-rate-limited model and pushes overflow onto fallbacks
that can end up saturated too. order_by_health() lets callers build a fresh
FallbackModel per call with recently-failed models pushed to the back --
never dropped (fail open), just deprioritized until their cooldown expires.
"""
from __future__ import annotations

import json
import time

import httpx

COOLDOWN_SECONDS = 20.0

_cooldown_until: dict[str, float] = {}


def mark_cooldown(model_name: str, seconds: float = COOLDOWN_SECONDS) -> None:
    _cooldown_until[model_name] = time.monotonic() + seconds


def is_cooling_down(model_name: str) -> bool:
    deadline = _cooldown_until.get(model_name)
    return deadline is not None and time.monotonic() < deadline


def order_by_health(model_names: list[str]) -> list[str]:
    """Not-cooling-down models first, cooling-down ones last -- relative order
    preserved within each group, no model ever dropped."""
    healthy = [m for m in model_names if not is_cooling_down(m)]
    cooling = [m for m in model_names if is_cooling_down(m)]
    return healthy + cooling


async def response_hook(response: httpx.Response) -> None:
    """httpx event hook: on a 429, cool down the model named in the request body.

    Marks cooldown under the "groq:<model>" spec form (matching CHATBOT_MODEL /
    CHATBOT_FALLBACK_MODEL in .env and agent.py's _MODEL/_FALLBACK_MODELS) so
    order_by_health() -- which operates on those same spec strings -- sees it.
    """
    if response.status_code != 429:
        return
    try:
        body = json.loads(response.request.content)
        model_name = body.get("model")
    except (ValueError, AttributeError):
        model_name = None
    if model_name:
        mark_cooldown(f"groq:{model_name}")
