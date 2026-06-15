"""Run one chat prompt through the real agent and capture everything the
evaluators need: reply text, ordered tool calls, retry count, token usage."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart

from services.chatbot_svc.agent import agent
from services.chatbot_svc.deps import build_deps


@dataclass
class ChatRunOutput:
    reply: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None  # exception class name when the run crashed

    def tool_names(self) -> list[str]:
        return [c["tool_name"] for c in self.tool_calls]


def extract_tool_calls(messages: list) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                args = part.args
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                calls.append({"tool_name": part.tool_name, "args": args})
    return calls


def count_retries(messages: list) -> int:
    return sum(
        1
        for msg in messages
        for part in getattr(msg, "parts", [])
        if isinstance(part, RetryPromptPart)
    )


async def run_chat_case(prompt: str, shop_domain: str, session_id: str) -> ChatRunOutput:
    """Run the agent on one prompt. Never raises — failures land in .error."""
    out = ChatRunOutput()
    deps = None
    try:
        deps = build_deps(shop_domain=shop_domain, user_id=None, session_id=session_id)
        result = await agent.run(prompt, deps=deps)
        messages = result.all_messages()
        usage = result.usage()
        out.reply = result.output
        out.tool_calls = extract_tool_calls(messages)
        out.retries = count_retries(messages)
        out.input_tokens = usage.input_tokens or 0
        out.output_tokens = usage.output_tokens or 0
    except Exception as exc:  # noqa: BLE001 — eval must survive any case crash
        out.error = type(exc).__name__
    finally:
        if deps is not None:
            await deps.http.aclose()
    return out
