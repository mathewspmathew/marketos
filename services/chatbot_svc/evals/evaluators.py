"""Five evaluation layers as pure check functions + pydantic-evals wrappers.

Pure functions take (ChatRunOutput, case-metadata dict) -> bool so they are
unit-testable without pydantic-evals plumbing. The Evaluator dataclasses are
thin adapters: ctx.output is the ChatRunOutput, ctx.metadata the dict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from services.chatbot_svc.evals.runner import ChatRunOutput

# Currency-marked (₹ / $ / Rs) or two-decimal amounts. Deliberately ignores
# bare integers ("172 pages") and unit decimals ("0.5mm").
_PRICE_RE = re.compile(
    r"(?:[₹$]|rs\.?\s*)\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)|(?<![\d.])(\d{1,3}(?:,\d{3})*\.\d{2})(?![\d])",
    re.IGNORECASE,
)
_CLAIMS_APPLIED_RE = re.compile(
    r"\b(i\s+have|i've|i\s+just)\s+(enabled|disabled|applied|changed|updated|set)\b",
    re.IGNORECASE,
)
_KNOWN_RULES = frozenset({
    "toggle_needs_preview",
    "price_change_needs_preview",
    "no_claim_applied",
    "must_ask_when_ambiguous",
})


def extract_prices(text: str) -> list[float]:
    prices: list[float] = []
    for m in _PRICE_RE.finditer(text):
        raw = (m.group(1) or m.group(2)).replace(",", "")
        prices.append(float(raw))
    return prices

# is each expected fact a substring of the reply?
def check_output_correctness(out: ChatRunOutput, meta: dict) -> bool:
    reply = out.reply.lower()
    return all(f.lower() in reply for f in meta.get("expected_facts", []))


def check_structured_output(out: ChatRunOutput, meta: dict) -> bool:
    return out.retries == 0 and out.error is None


def check_tool_selection(out: ChatRunOutput, meta: dict) -> bool:
    called = set(out.tool_names())
    expected = set(meta.get("expected_tools", []))
    forbidden = set(meta.get("forbidden_tools", []))
    return expected.issubset(called) and not (forbidden & called)


def check_price_hallucination(out: ChatRunOutput, meta: dict) -> bool:
    """True = clean (no hallucinated price). Tolerates rounding to 2dp."""
    allowed = meta.get("allowed_prices", [])
    return all(
        any(abs(p - a) < 0.01 for a in allowed) for p in extract_prices(out.reply)
    )


def check_business_logic(out: ChatRunOutput, meta: dict) -> bool:
    called = set(out.tool_names())
    for rule in meta.get("rules", []):
        if rule not in _KNOWN_RULES:
            raise ValueError(f"unknown business-logic rule: {rule!r}")
        if rule == "toggle_needs_preview" and "preview_dynamic_pricing_toggle" not in called:
            return False
        if rule == "price_change_needs_preview" and "preview_price_change" not in called:
            return False
        if rule == "no_claim_applied" and _CLAIMS_APPLIED_RE.search(out.reply):
            return False
        if rule == "must_ask_when_ambiguous" and "ask_user" not in called:
            return False
    return True


@dataclass
class OutputCorrectness(Evaluator):
    def get_default_evaluation_name(self) -> str:
        return "output_correctness"

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return check_output_correctness(ctx.output, ctx.metadata or {})


@dataclass
class StructuredOutput(Evaluator):
    def get_default_evaluation_name(self) -> str:
        return "structured_output"

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return check_structured_output(ctx.output, ctx.metadata or {})


@dataclass
class ToolSelection(Evaluator):
    def get_default_evaluation_name(self) -> str:
        return "tool_selection"

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return check_tool_selection(ctx.output, ctx.metadata or {})


@dataclass
class PriceHallucination(Evaluator):
    def get_default_evaluation_name(self) -> str:
        return "price_hallucination"

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return check_price_hallucination(ctx.output, ctx.metadata or {})


@dataclass
class BusinessLogic(Evaluator):
    def get_default_evaluation_name(self) -> str:
        return "business_logic"

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return check_business_logic(ctx.output, ctx.metadata or {})
