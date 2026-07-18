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

import sys
import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.fallback import FallbackModel

# Observability: write the agent's span tree to stderr, and upload to Logfire
# when LOGFIRE_TOKEN is set (see .env / docker-compose). Environments without
# the token (CI, teammates) fall back to console-only.
logfire.configure(
    send_to_logfire="if-token-present",
    service_name="chatbot_svc",
    console=logfire.ConsoleOptions(
        output=sys.stderr,
        colors="never",
        span_style="show-parents",
        include_timestamps=True,
        verbose=False,
        min_log_level="debug",
    ),
)
logfire.instrument_pydantic_ai()

from services.chatbot_svc.deps import AgentDeps
from services.chatbot_svc.schemas import (
    ScopeFilter,
    PriceChange,
    PreviewSummary,
    PanelSummary,  # noqa: F401 — kept for the commented-out open_dynamic_pricing_panel tool below
    ApplyResult,
    VariantSummary,
    ResolvedProduct,
    DynamicPricingStatus,
    DiscoveryDebugInfo,
    PriceExplanation,
    MatchExplanation,
    PaneConfigInput,
    ApplyPaneConfigResult,
    PauseDynamicPricingResult,
    DeleteDynamicPricingResult,
)
from services.chatbot_svc.tools import search as t_search
from services.chatbot_svc.tools import stats as t_stats
from services.chatbot_svc.tools import preview as t_preview
from services.chatbot_svc.tools import panel as t_panel  # noqa: F401 — kept for the commented-out open_dynamic_pricing_panel tool below
from services.chatbot_svc.tools import apply_config as t_apply_config
from services.chatbot_svc.tools import status as t_status
from services.chatbot_svc.tools import debug as t_debug
from services.chatbot_svc.tools import price_explanation as t_price_explanation
from services.chatbot_svc.tools import match_explanation as t_match_explanation
from services.chatbot_svc.tools import resolution_guard as t_resolution_guard
from services.chatbot_svc.tools.ask import ask_user as _ask_user_raw


_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text()

# Both models come from .env — no model names are hardcoded here.
#   CHATBOT_MODEL          (required)  e.g. groq:openai/gpt-oss-120b
#   CHATBOT_FALLBACK_MODEL (optional)  requests that fail on the primary
#                          (429 quota, 5xx) transparently retry on this model.
#                          Leave unset for eval runs (single-model scoring).
load_dotenv()
_MODEL = os.environ.get("CHATBOT_MODEL")
if not _MODEL:
    raise RuntimeError("CHATBOT_MODEL is not set — add it to .env, e.g. CHATBOT_MODEL=groq:openai/gpt-oss-120b")
_FALLBACK_MODEL = os.environ.get("CHATBOT_FALLBACK_MODEL")

_model = FallbackModel(_MODEL, _FALLBACK_MODEL) if _FALLBACK_MODEL else _MODEL

agent: Agent[AgentDeps, str] = Agent(
    _model,
    deps_type=AgentDeps,
    system_prompt=_PROMPT,
)


@agent.tool
def resolve_product(
    ctx: RunContext[AgentDeps],
    reference: str,
) -> list[ResolvedProduct]:
    """Resolve a product the user named (e.g. 'Luxury Tailored Pant') to REAL products
    in this shop. Call this BEFORE any preview/apply when the user refers to a product,
    and use ONLY the product_id / variant_ids it returns — never invent ids.
    Returns: [] = not found (tell the user); 1 item = use it; >1 = call ask_user to pick."""
    results = t_search.resolve_product(ctx.deps.shop_domain, reference)
    t_resolution_guard.record_resolved_products(
        ctx.deps.session_id, [r.product_id for r in results]
    )
    return results


@agent.tool
def get_dynamic_pricing_status(
    ctx: RunContext[AgentDeps],
    product_id: str,
) -> DynamicPricingStatus | None:
    """Report where a product stands in the dynamic-pricing pipeline (OFF / SETTING_UP /
    DISCOVERING / PROCESSING / READY / NEEDS_ATTENTION) with competitor + match counts.
    Call this with a product_id from resolve_product before enabling/changing dynamic pricing.
    Returns None if the product_id is not in this shop."""
    return t_status.get_dynamic_pricing_status(ctx.deps.shop_domain, product_id)


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
    """Preview a price change on the matched variants. Surfaces a preview card whose Apply button performs the change (the agent does not apply prices itself)."""
    return t_preview.preview_price_change(
        ctx.deps.shop_domain,
        ctx.deps.session_id,
        scope,
        change,
    )


# Retired from the chat tool surface (commented out, not deleted — kept in
# case the direct-tool approach (apply/pause/delete, all no-card) needs a
# card-based fallback again later). Delete/resume/ambiguous requests now
# route to delete_dynamic_pricing / apply_dynamic_pricing_config / a plain
# get_dynamic_pricing_status answer instead — see the Hard rule decision
# procedure in prompts/system.md. The underlying implementation in
# tools/panel.py is untouched.
#
# @agent.tool
# def open_dynamic_pricing_panel(
#     ctx: RunContext[AgentDeps],
#     product_id: str,
# ) -> PanelSummary:
#     """Open the dynamic-pricing panel card for ONE product."""
#     return t_panel.open_dynamic_pricing_panel(
#         ctx.deps.shop_domain, ctx.deps.session_id, product_id
#     )


@agent.tool
def apply_dynamic_pricing_config(
    ctx: RunContext[AgentDeps],
    product_id: str,
    config: PaneConfigInput,
) -> ApplyPaneConfigResult:
    """Turn on / update / resume dynamic pricing for ONE product using the
    configuration values the user actually specified in their message
    (search query, pricing tier, min/max price, rescrape frequency,
    discovery settings). Call this for ANY turn-on/enable/update/resume
    request, with or without concrete values — this applies the change
    directly, with no card or confirmation click. Only include fields the
    user mentioned; omitted fields keep the product's existing value, they
    are not reset. A plain resume ("resume dynamic pricing on X") is just
    this tool called with every field omitted. On a product that has NEVER
    been configured before, pricing_tier and both frequency fields are
    required — if the merchant's message doesn't include them, ask for the
    missing value(s) with ask_user before calling this tool, don't guess or
    omit them (a previously-paused product with existing config does not
    need pricing_tier or frequency re-asked). If pricing tier is missing,
    ask_user's options MUST be ["BUDGET", "COMPETITIVE", "PREMIUM"]. Call
    resolve_product first to get product_id.
    Raises an error if product_id is not in this shop, or if the config is
    invalid (e.g. min price >= max price)."""
    return t_apply_config.apply_dynamic_pricing_config(
        ctx.deps.shop_domain, product_id, ctx.deps.session_id, config
    )


@agent.tool
def pause_dynamic_pricing(
    ctx: RunContext[AgentDeps],
    product_id: str,
) -> PauseDynamicPricingResult:
    """Immediately pause dynamic pricing for ONE product — flips the flag
    off, keeps all pane configuration (tier, price bounds, frequency, etc.)
    intact so it can be resumed later. Applies directly, no card or
    confirmation click. Call this ONLY for a clear pause/stop request with
    nothing else specified. For resume, use apply_dynamic_pricing_config
    (with every field omitted) instead. For delete, use get_delete_preview
    then delete_dynamic_pricing. Call resolve_product first to get
    product_id. Raises an error if product_id is not in this shop."""
    return t_apply_config.pause_dynamic_pricing(ctx.deps.shop_domain, product_id, ctx.deps.session_id)


@agent.tool
def get_delete_preview(
    ctx: RunContext[AgentDeps],
    product_id: str,
) -> dict:
    """Preview what deleting dynamic-pricing data for ONE product would
    remove — competitor_products, discovered_links, price_stats_variants
    (all counts, read-only, no DB write). Call this BEFORE warning the
    merchant and asking for delete confirmation."""
    return t_apply_config.get_delete_preview(ctx.deps.shop_domain, product_id)


@agent.tool
def delete_dynamic_pricing(
    ctx: RunContext[AgentDeps],
    product_id: str,
    confirmed: bool,
) -> DeleteDynamicPricingResult:
    """Permanently delete ALL dynamic-pricing data for ONE product (competitor
    products, matches, price history) and reset its pane config to defaults.
    THIS CANNOT BE UNDONE. You MUST call get_delete_preview first, warn the
    merchant with its real counts via ask_user, and only call this tool with
    confirmed=True after they explicitly agree in a following message. Never
    call this with confirmed=True without a real prior confirmation. Call
    resolve_product first to get product_id. Raises an error if product_id is
    not in this shop, or if confirmed is False."""
    return t_apply_config.delete_dynamic_pricing(
        ctx.deps.shop_domain, product_id, ctx.deps.session_id, confirmed
    )


@agent.tool
def ask_user(
    ctx: RunContext[AgentDeps],
    question: str,
    options: list[str] | None = None,
) -> str:
    """Pause the agent and surface a clarification question to the merchant."""
    return _ask_user_raw(question, options)


@agent.tool
def debug_discovery(
    ctx: RunContext[AgentDeps],
    product_id: str,
) -> DiscoveryDebugInfo | None:
    """Troubleshoot discovery for a product. Shows: candidates found/scraped/verified/rejected/dead,
    matches made, errors, and recommended next action. Use this when a product has no competitors
    or discovery is stuck. Returns None if the product has no discovery job."""
    return t_debug.debug_discovery(ctx.deps.shop_domain, product_id)


@agent.tool
def explain_price_decision(
    ctx: RunContext[AgentDeps],
    variant_id: str,
) -> PriceExplanation | None:
    """Explain why a price was recommended for a variant. Returns competitor context (median/mean/min/max prices),
    the price delta, and human-readable explanation. Call this when a merchant asks 'why was this price recommended?'
    Returns None if no price decision exists for this variant."""
    return t_price_explanation.explain_price_decision(ctx.deps.shop_domain, variant_id)


@agent.tool
def explain_product_match(
    ctx: RunContext[AgentDeps],
    variant_id: str,
) -> MatchExplanation | None:
    """Explain how a competitor was matched to your product. Shows: competitor details,
    confidence tier (CONFIRMED/LIKELY/WEAK), match score, and reasoning. Use this when a merchant
    asks 'how did you find this competitor?' or 'why is this match valid?'
    Returns None if no match exists for this variant."""
    return t_match_explanation.explain_product_match(ctx.deps.shop_domain, variant_id)
