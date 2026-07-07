"""Run one chat prompt through the real agent and capture everything the
evaluators need: reply text, ordered tool calls, retry count, token usage."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import capture_run_messages
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart

from services.chatbot_svc.agent import agent
from services.chatbot_svc.deps import build_deps
from services.chatbot_svc.tools.ask import AskUserRequested


@dataclass
class ChatRunOutput:
    reply: str = ""
    ask: str | None = None  # set when the agent paused to ask a clarifying question
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)  # tool names that returned is_error=True
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


def extract_tool_errors(messages: list) -> list[str]:
    """Return names of tools whose ToolReturnPart has is_error=True."""
    errors: list[str] = []
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart) and getattr(part, "is_error", False):
                errors.append(part.tool_name)
    return errors


def count_retries(messages: list) -> int:
    return sum(
        1
        for msg in messages
        for part in getattr(msg, "parts", [])
        if isinstance(part, RetryPromptPart)
    )


async def run_chat_case(prompt: str, shop_domain: str, session_id: str) -> ChatRunOutput:
    """Run the agent on one prompt. Never raises — asks land in .ask,
    crashes in .error."""
    out = ChatRunOutput()
    deps = None
    try:
        deps = build_deps(shop_domain=shop_domain, user_id=None, session_id=session_id)
        # capture_run_messages keeps the message log even when agent.run raises,
        # so tool calls made before an ask_user are not lost
        with capture_run_messages() as messages:
            try:
                result = await agent.run(prompt, deps=deps)
            except AskUserRequested as ask:
                out.ask = ask.question
                out.tool_calls = extract_tool_calls(messages)
                out.tool_errors = extract_tool_errors(messages)
                out.retries = count_retries(messages)
                return out
        messages = result.all_messages()
        usage = result.usage()
        out.reply = result.output
        out.tool_calls = extract_tool_calls(messages)
        out.tool_errors = extract_tool_errors(messages)
        out.retries = count_retries(messages)
        out.input_tokens = usage.input_tokens or 0
        out.output_tokens = usage.output_tokens or 0
    except Exception as exc:  # noqa: BLE001 — eval must survive any case crash
        out.error = type(exc).__name__
    finally:
        if deps is not None:
            await deps.http.aclose()
    return out
