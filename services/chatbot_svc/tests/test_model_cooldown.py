"""Tests for services/chatbot_svc/model_cooldown.py -- the in-process
tracker that stops FallbackModel from retrying an already-rate-limited
model on every single agent.run() call."""
from __future__ import annotations

import json

import httpx
import pytest

from services.chatbot_svc import model_cooldown


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    model_cooldown._cooldown_until.clear()
    yield
    model_cooldown._cooldown_until.clear()


def test_order_by_health_preserves_order_when_nothing_cooling():
    models = ["groq:a", "groq:b", "groq:c"]
    assert model_cooldown.order_by_health(models) == models


def test_order_by_health_pushes_cooling_model_to_back_without_dropping_it():
    model_cooldown.mark_cooldown("groq:b")
    ordered = model_cooldown.order_by_health(["groq:a", "groq:b", "groq:c"])
    assert ordered == ["groq:a", "groq:c", "groq:b"]


def test_order_by_health_all_cooling_still_returns_everything():
    for m in ["groq:a", "groq:b"]:
        model_cooldown.mark_cooldown(m)
    ordered = model_cooldown.order_by_health(["groq:a", "groq:b"])
    assert set(ordered) == {"groq:a", "groq:b"}


def test_cooldown_expires_after_window(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(model_cooldown.time, "monotonic", lambda: t[0])
    model_cooldown.mark_cooldown("groq:a", seconds=10)
    assert model_cooldown.is_cooling_down("groq:a") is True
    t[0] += 11
    assert model_cooldown.is_cooling_down("groq:a") is False


def _fake_response(status_code: int, model: str) -> httpx.Response:
    request = httpx.Request(
        "POST",
        "https://api.groq.com/openai/v1/chat/completions",
        content=json.dumps({"model": model}).encode(),
    )
    return httpx.Response(status_code, request=request)


async def test_response_hook_marks_cooldown_on_429():
    await model_cooldown.response_hook(_fake_response(429, "openai/gpt-oss-20b"))
    assert model_cooldown.is_cooling_down("groq:openai/gpt-oss-20b") is True


async def test_response_hook_ignores_success():
    await model_cooldown.response_hook(_fake_response(200, "openai/gpt-oss-20b"))
    assert model_cooldown.is_cooling_down("groq:openai/gpt-oss-20b") is False
