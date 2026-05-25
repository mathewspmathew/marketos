"""Pydantic-AI agent wiring all chatbot tools.

API notes (pydantic-ai==1.102.0):
- Agent constructor: Agent(model, *, deps_type, system_prompt, ...)
- @agent.tool decorator registers functions that receive RunContext[T] as their
  first argument (sync or async both supported).
- @agent.tool_plain registers functions with no RunContext (not used here).
- RunContext is imported from pydantic_ai directly (not pydantic_ai.tools).
- Model identifiers are provider-prefixed strings: "groq:<model-name>".
- Tool registry lives at agent._function_toolset.tools (dict[str, Tool]).
  Exposed publicly via: list(agent._function_toolset.tools)
"""
from __future__ import annotations

import os
from pathlib import Path

import logfire
from pydantic_ai import Agent, RunContext

# Observability — silent no-op if LOGFIRE_TOKEN is unset, so this is safe
# in CI/tests. Send_to_logfire="if-token-present" avoids interactive prompts.
logfire.configure(
    send_to_logfire="if-token-present",
    service_name="chatbot_svc",
    console=False,
)
logfire.instrument_pydantic_ai()

from services.chatbot_svc.deps import AgentDeps
from services.chatbot_svc.schemas import (
    ScopeFilter,
    PriceChange,
    PreviewSummary,
    ApplyResult,
    VariantSummary,
)
from services.chatbot_svc.tools import search as t_search
from services.chatbot_svc.tools import stats as t_stats
from services.chatbot_svc.tools import preview as t_preview
from services.chatbot_svc.tools import apply as t_apply
from services.chatbot_svc.tools.ask import ask_user as _ask_user_raw


_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text()
_MODEL = os.environ.get("CHATBOT_MODEL", "groq:llama-3.3-70b-versatile")

agent: Agent[AgentDeps, str] = Agent(
    _MODEL,
    deps_type=AgentDeps,
    system_prompt=_PROMPT,
)


@agent.tool
def structured_search(
    ctx: RunContext[AgentDeps],
    scope: ScopeFilter,
    limit: int = 25,
) -> list[VariantSummary]:
    """Search merchant variants by structured filters (vendor, product type, tags, etc.)."""
    return t_search.structured_search(ctx.deps.shop_domain, scope, limit=limit)


@agent.tool
def semantic_search(
    ctx: RunContext[AgentDeps],
    query: str,
    top_k: int = 20,
) -> list[VariantSummary]:
    """Vector similarity search over merchant variants using natural language query."""
    return t_search.semantic_search(ctx.deps.shop_domain, query, top_k=top_k)


@agent.tool
def get_variant(
    ctx: RunContext[AgentDeps],
    variant_id: str,
) -> VariantSummary | None:
    """Fetch a single variant by its Shopify variant id, scoped to the current shop."""
    return t_search.get_variant(ctx.deps.shop_domain, variant_id)


@agent.tool
def get_stats(
    ctx: RunContext[AgentDeps],
    metric: t_stats.StatsMetric,
    scope: ScopeFilter | None = None,
) -> dict:
    """Run a read-only analytical query and return pricing/coverage statistics."""
    return t_stats.get_stats(ctx.deps.shop_domain, metric, scope)


@agent.tool
def preview_price_change(
    ctx: RunContext[AgentDeps],
    scope: ScopeFilter,
    change: PriceChange,
) -> PreviewSummary:
    """Preview a price change on the matched variants — MUST be called before apply_price_change."""
    return t_preview.preview_price_change(
        ctx.deps.shop_domain,
        ctx.deps.session_id,
        scope,
        change,
    )


@agent.tool
def preview_dynamic_pricing_toggle(
    ctx: RunContext[AgentDeps],
    scope: ScopeFilter,
    enabled: bool,
) -> PreviewSummary:
    """Preview enabling/disabling dynamic pricing on matched products — MUST be called before apply_dynamic_pricing_toggle."""
    return t_preview.preview_dynamic_pricing_toggle(
        ctx.deps.shop_domain,
        ctx.deps.session_id,
        scope,
        enabled,
    )


@agent.tool
async def apply_price_change(
    ctx: RunContext[AgentDeps],
    preview_id: str,
) -> ApplyResult:
    """Apply a previously previewed price change. Requires preview_id from preview_price_change."""
    return await t_apply.apply_price_change(ctx.deps, preview_id)


@agent.tool
async def apply_dynamic_pricing_toggle(
    ctx: RunContext[AgentDeps],
    preview_id: str,
) -> ApplyResult:
    """Apply a previously previewed dynamic-pricing toggle. Requires preview_id from preview_dynamic_pricing_toggle."""
    return await t_apply.apply_dynamic_pricing_toggle(ctx.deps, preview_id)


@agent.tool
def ask_user(
    ctx: RunContext[AgentDeps],
    question: str,
    options: list[str] | None = None,
) -> str:
    """Pause the agent and surface a clarification question to the merchant."""
    return _ask_user_raw(question, options)
