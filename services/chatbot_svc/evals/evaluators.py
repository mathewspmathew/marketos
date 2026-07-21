"""Evaluation layers as pure check functions + pydantic-evals wrappers.

Pure functions take (ChatRunOutput, case-metadata dict) -> (bool|float, reason)
so they are unit-testable without pydantic-evals plumbing, and every verdict
explains itself.  The Evaluator dataclasses are thin adapters: ctx.output is
the ChatRunOutput, ctx.metadata the dict; they wrap the result in
EvaluationReason so the reason shows up in Logfire and the report.

OutputCorrectness uses an LLM judge (see judge.py) and its Evaluator wrapper
is therefore async — pydantic-evals supports both sync and async evaluate().
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext

from services.chatbot_svc.evals.judge import llm_judge
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


async def check_output_correctness(
    out: ChatRunOutput,
    meta: dict,
    user_prompt: str = "",
) -> tuple[float, str]:
    """LLM-as-judge: score 0.0–1.0 for how well the reply satisfies the case criteria.

    Passes the user prompt, agent reply, and expected_facts to the judge model
    (CHATBOT_EVAL_MODEL).  Returns (score, one-sentence reasoning).
    """
    criteria = meta.get("expected_facts", [])
    reply = out.reply or out.ask or ""
    return await llm_judge(user_prompt, reply, criteria)


def check_structured_output(out: ChatRunOutput, meta: dict) -> tuple[bool, str]:
    if out.retries == 0 and out.error is None:
        return True, "no validation retries, no crash"
    return False, f"retries={out.retries}, error={out.error}"


def check_tool_selection(out: ChatRunOutput, meta: dict) -> tuple[bool, str]:
    called = set(out.tool_names())
    missing = set(meta.get("expected_tools", [])) - called
    hit_forbidden = set(meta.get("forbidden_tools", [])) & called
    if missing:
        return False, f"expected tools never called: {sorted(missing)}"
    if hit_forbidden:
        return False, f"forbidden tools were called: {sorted(hit_forbidden)}"
    return True, f"all expected tools called, no forbidden ones (called: {out.tool_names()})"


def check_price_hallucination(out: ChatRunOutput, meta: dict) -> tuple[bool, str]:
    """True = clean (no hallucinated price). Tolerates rounding to 2dp."""
    allowed = meta.get("allowed_prices", [])
    mentioned = extract_prices(out.reply)
    if not mentioned:
        return True, "reply mentions no prices"
    bad = [p for p in mentioned if not any(abs(p - a) < 0.01 for a in allowed)]
    if bad:
        return False, f"reply mentions prices that are not in the store: {bad}"
    return True, f"all mentioned prices exist in the store: {mentioned}"


def check_business_logic(out: ChatRunOutput, meta: dict) -> tuple[bool, str]:
    called = set(out.tool_names())
    rules = meta.get("rules", [])
    for rule in rules:
        if rule not in _KNOWN_RULES:
            raise ValueError(f"unknown business-logic rule: {rule!r}")
        if rule == "price_change_needs_preview" and "preview_price_change" not in called:
            return False, "price_change_needs_preview: preview_price_change was never called"
        if rule == "no_claim_applied" and _CLAIMS_APPLIED_RE.search(out.reply):
            return False, "no_claim_applied: reply claims a change was already applied"
        if rule == "must_ask_when_ambiguous" and "ask_user" not in called:
            return False, "must_ask_when_ambiguous: ask_user was never called"
    if not rules:
        return True, "no business rules on this case"
    return True, f"all business rules satisfied: {rules}"


def check_tool_recall(out: ChatRunOutput, meta: dict) -> tuple[float, str]:
    """Recall = |called ∩ expected| / |expected|.  1.0 when expected is empty."""
    expected = set(meta.get("expected_tools", []))
    if not expected:
        return 1.0, "no expected tools defined — recall trivially 1.0"
    called = set(out.tool_names())
    hit = expected & called
    score = round(len(hit) / len(expected), 3)
    missed = sorted(expected - called)
    if missed:
        return score, f"recall={score} — missed tools: {missed} (called: {sorted(called)})"
    return score, f"recall=1.0 — all expected tools called"


def check_tool_precision(out: ChatRunOutput, meta: dict) -> tuple[float, str]:
    """Precision = |called ∩ expected| / |called|.  1.0 when nothing called."""
    expected = set(meta.get("expected_tools", []))
    called_names = out.tool_names()
    called = set(called_names)
    if not called:
        return 1.0, "no tools called — precision trivially 1.0"
    hit = expected & called
    score = round(len(hit) / len(called), 3)
    spurious = sorted(called - expected)
    if spurious:
        return score, f"precision={score} — spurious tools: {spurious}"
    return score, f"precision=1.0 — no spurious tools called"


def check_tool_success(out: ChatRunOutput, meta: dict) -> tuple[bool, str]:
    """True when no tool returned is_error=True during the run."""
    if not out.tool_errors:
        return True, "all tool calls succeeded (no tool errors)"
    return False, f"tools returned errors: {out.tool_errors}"


@dataclass
class OutputCorrectness(Evaluator):
    """LLM-as-judge evaluator. Returns a float score 0.0–1.0."""

    def get_default_evaluation_name(self) -> str:
        return "output_correctness"

    async def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        score, why = await check_output_correctness(
            ctx.output,
            ctx.metadata or {},
            user_prompt=ctx.inputs or "",
        )
        return EvaluationReason(value=score, reason=why)


@dataclass
class StructuredOutput(Evaluator):
    def get_default_evaluation_name(self) -> str:
        return "structured_output"

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        ok, why = check_structured_output(ctx.output, ctx.metadata or {})
        return EvaluationReason(value=ok, reason=why)


@dataclass
class ToolSelection(Evaluator):
    def get_default_evaluation_name(self) -> str:
        return "tool_selection"

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        ok, why = check_tool_selection(ctx.output, ctx.metadata or {})
        return EvaluationReason(value=ok, reason=why)


@dataclass
class PriceHallucination(Evaluator):
    def get_default_evaluation_name(self) -> str:
        return "price_hallucination"

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        ok, why = check_price_hallucination(ctx.output, ctx.metadata or {})
        return EvaluationReason(value=ok, reason=why)


@dataclass
class BusinessLogic(Evaluator):
    def get_default_evaluation_name(self) -> str:
        return "business_logic"

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        ok, why = check_business_logic(ctx.output, ctx.metadata or {})
        return EvaluationReason(value=ok, reason=why)


@dataclass
class ToolRecall(Evaluator):
    """Fraction of expected tools that were actually called (0–1)."""

    def get_default_evaluation_name(self) -> str:
        return "tool_recall"

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        score, why = check_tool_recall(ctx.output, ctx.metadata or {})
        return EvaluationReason(value=score, reason=why)


@dataclass
class ToolPrecision(Evaluator):
    """Fraction of called tools that were expected (no spurious calls)."""

    def get_default_evaluation_name(self) -> str:
        return "tool_precision"

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        score, why = check_tool_precision(ctx.output, ctx.metadata or {})
        return EvaluationReason(value=score, reason=why)


@dataclass
class ToolSuccess(Evaluator):
    """True when no tool returned an error during the run."""

    def get_default_evaluation_name(self) -> str:
        return "tool_success"

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        ok, why = check_tool_success(ctx.output, ctx.metadata or {})
        return EvaluationReason(value=ok, reason=why)
