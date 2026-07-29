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
import re
from pathlib import Path

import sys
import logfire
from dotenv import load_dotenv
from groq import AsyncGroq
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.providers.groq import GroqProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

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
#   CHATBOT_FALLBACK_MODEL (optional)  comma-separated list, tried in order
#                          when a request fails on the model before it
#                          (429 quota, 5xx). Each entry should be a model
#                          you haven't already hit quota on today — Groq
#                          tracks TPD/RPD per exact model string, so a
#                          fresh model name means a fresh daily budget.
load_dotenv()
_MODEL = os.environ.get("CHATBOT_MODEL")
if not _MODEL:
    raise RuntimeError("CHATBOT_MODEL is not set — add it to .env, e.g. CHATBOT_MODEL=groq:openai/gpt-oss-120b")
_FALLBACK_MODELS = [m.strip() for m in os.environ.get("CHATBOT_FALLBACK_MODEL", "").split(",") if m.strip()]

# Dedicated key so the chatbot's TPM budget is independent from extraction /
# semantics (services/common/groq_client.py). Falls back to GROQ_API_KEY if unset.
#
# max_retries=1 (not the SDK's default of 2, not 0): the default retries a 429
# in place, sleeping for the server's Retry-After (observed 7-14s per attempt)
# before trying the SAME rate-limited model again -- this silently ate ~70s on
# one real request while FallbackModel below never got a chance to switch
# models. But max_retries=0 is too aggressive: the dev multi-turn eval
# (run_multiturn.py) caught it live -- under real quota pressure, a single
# transient 429 with zero cushion failed the ENTIRE fallback chain at once
# (FallbackExceptionGroup) instead of the primary model just recovering after
# one short wait. One bounded retry is the middle ground.
_CHATBOT_GROQ_PROVIDER = GroqProvider(
    groq_client=AsyncGroq(
        api_key=os.environ.get("GROQ_API_KEY_CHATBOT") or os.environ.get("GROQ_API_KEY"),
        max_retries=1,
    )
)


def _as_model(spec: str) -> GroqModel:
    """Turn a 'groq:<model-name>' spec into a GroqModel using the chatbot's own key."""
    provider, model_name = spec.split(":", 1)
    if provider != "groq":
        raise ValueError(f"Only groq: models are supported, got {spec!r}")
    return GroqModel(model_name, provider=_CHATBOT_GROQ_PROVIDER)


_model = (
    FallbackModel(_as_model(_MODEL), *[_as_model(m) for m in _FALLBACK_MODELS])
    if _FALLBACK_MODELS
    else _as_model(_MODEL)
)

agent: Agent[AgentDeps, str] = Agent(
    _model,
    deps_type=AgentDeps,
    system_prompt=_PROMPT,
    # Low, not zero: this is a tool-calling agent that must state real data
    # accurately, not write creatively. Prompt-level grounding (hard rules,
    # the output validator below) holds up far better at low temperature --
    # it visibly failed at this model's default (see the fabricated
    # "Pigeon T-shirt" incident, zero tool calls behind the answer).
    model_settings=ModelSettings(temperature=0.2),
)

# Bounds one agent.run() call: caps both LLM round trips and successful tool
# executions, so a model stuck re-querying for a product it can't find (or
# hallucinated) fails fast instead of looping silently -- one real incident
# looped structured_search 6x chasing a nonexistent product, each call also
# eating a 429 retry, for ~70s total before finally answering anyway.
CHAT_USAGE_LIMITS = UsageLimits(request_limit=10, tool_calls_limit=6)

# Tools whose result can ground a claim about a specific product/variant
# (price, id, title, status). Any final answer that states such a claim
# without one of these having been called THIS run is unverified.
_GROUNDING_TOOLS = frozenset(
    {
        "resolve_product",
        "get_variant",
        "structured_search",
        "semantic_search",
        "get_stats",
        "get_dynamic_pricing_status",
        "debug_discovery",
        "get_delete_preview",
        "explain_price_decision",
        "explain_product_match",
    }
)

# A currency amount: symbol+digits ($480, ₹1,200.50), or a code+digits (INR 480).
_PRICE_CLAIM_RE = re.compile(r"[$₹€£]\s?\d|\b(?:INR|USD|EUR|GBP)\s?\d", re.IGNORECASE)

# Tools whose result names real, resolvable products/variants -- when either
# was called this run, an ask_user disambiguation's options should be drawn
# from what they actually returned, not invented.
_CANDIDATE_TOOLS = frozenset({"resolve_product", "structured_search"})


def _candidate_titles(messages: list) -> set[str]:
    """Titles from any resolve_product/structured_search result this run."""
    titles: set[str] = set()
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if not (isinstance(part, ToolReturnPart) and part.tool_name in _CANDIDATE_TOOLS):
                continue
            content = part.content
            if not isinstance(content, list):
                continue
            for item in content:
                title = item.get("title") if isinstance(item, dict) else getattr(item, "title", None)
                if title:
                    titles.add(title)
    return titles


@agent.output_validator
def ensure_price_claims_are_grounded(ctx: RunContext[AgentDeps], output: str) -> str:
    """Reject a final answer that states a price without having called a
    grounding tool this run -- see the fabricated "Pigeon T-shirt" incident
    this guards against: the model answered a price question with zero tool
    calls, inventing both the product and the number."""
    if not _PRICE_CLAIM_RE.search(output):
        return output

    called = {
        part.tool_name
        for msg in ctx.messages
        for part in getattr(msg, "parts", [])
        if isinstance(part, ToolCallPart)
    }
    if called & _GROUNDING_TOOLS:
        return output

    raise ModelRetry(
        "You stated a price without calling a tool this turn. Call "
        "resolve_product / get_variant / structured_search (etc.) to get "
        "the real data first -- never state a price from memory or by "
        "pattern-matching an earlier answer."
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
    """Turn on / update / resume dynamic pricing for ONE product. Applies
    directly, no card. Include only the fields the merchant actually
    mentioned -- omitted fields keep the product's existing value, they are
    NOT reset. Call resolve_product first for product_id. See the system
    prompt's Hard rule decision procedure for when to ask_user first versus
    calling this directly. On a never-configured product missing
    pricing_tier, ask_user's options MUST be ["BUDGET", "COMPETITIVE", "PREMIUM"]
    -- never an open-ended tier question. Raises an error if product_id is
    not in this shop, or if the config is invalid (e.g. min price >= max price)."""
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
    if options:
        candidates = _candidate_titles(ctx.messages)
        # Only enforce when real candidates exist this run -- unrelated
        # ask_user calls (pricing tier, delete confirmation, etc.) never
        # have a resolve_product/structured_search result to draw from and
        # must not be blocked by this. See the "Which shirt are you
        # referring to?" incident this guards against: the model asked with
        # invented action-phrase options ("Show the shirt details", ...)
        # instead of the one real candidate resolve_product had returned.
        if candidates and not (set(options) & candidates):
            raise ModelRetry(
                "Your ask_user options don't match any real product title "
                "resolve_product/structured_search returned this turn. When "
                "asking the merchant to pick a PRODUCT, options must be "
                "exactly those candidate titles -- never invented action "
                "phrases or made-up names."
            )
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
