import pytest

from services.chatbot_svc.evals import evaluators as ev_module
from services.chatbot_svc.evals.evaluators import (
    BusinessLogic,
    OutputCorrectness,
    PriceHallucination,
    StructuredOutput,
    ToolPrecision,
    ToolRecall,
    ToolSelection,
    ToolSuccess,
    check_business_logic,
    check_output_correctness,
    check_price_hallucination,
    check_structured_output,
    check_tool_selection,
    extract_prices,
)
from services.chatbot_svc.evals.report import ALL_LAYERS
from services.chatbot_svc.evals.runner import ChatRunOutput


def _out(reply="", tools=(), retries=0, error=None):
    return ChatRunOutput(
        reply=reply,
        tool_calls=[{"tool_name": t, "args": {}} for t in tools],
        retries=retries,
        error=error,
    )


# Layer 1 — output correctness. check_output_correctness delegates scoring
# to the judge LLM (judge.py) — it's a thin async wrapper now, not a keyword
# matcher, so these test the wiring (what gets forwarded, what comes back),
# not the LLM's own judgment (that's exercised live by run.py).
async def test_correctness_forwards_prompt_reply_and_criteria_to_judge(monkeypatch):
    captured = {}
    async def fake_judge(prompt, reply, criteria):
        captured.update(prompt=prompt, reply=reply, criteria=criteria)
        return 0.85, "mock reasoning"
    monkeypatch.setattr(ev_module, "llm_judge", fake_judge)

    out = _out(reply="The Pilot V5 pen costs ₹175.00.")
    score, why = await check_output_correctness(out, {"expected_facts": ["Pilot", "175"]}, user_prompt="price?")

    assert score == 0.85 and why == "mock reasoning"
    assert captured == {"prompt": "price?", "reply": "The Pilot V5 pen costs ₹175.00.", "criteria": ["Pilot", "175"]}

async def test_correctness_uses_ask_when_reply_is_empty(monkeypatch):
    captured = {}
    async def fake_judge(prompt, reply, criteria):
        captured["reply"] = reply
        return 1.0, "ok"
    monkeypatch.setattr(ev_module, "llm_judge", fake_judge)

    out = ChatRunOutput(reply="", ask="Which product did you mean?", tool_calls=[])
    await check_output_correctness(out, {"expected_facts": []})

    assert captured["reply"] == "Which product did you mean?"


# Layer 2 — structured output
def test_structured_output_fails_on_retries_or_error():
    assert check_structured_output(_out(), {})[0]
    ok, why = check_structured_output(_out(retries=1), {})
    assert not ok and "retries=1" in why
    ok, why = check_structured_output(_out(error="ValidationError"), {})
    assert not ok and "ValidationError" in why


# Layer 3 — tool selection
def test_tool_selection_requires_expected_and_blocks_forbidden():
    out = _out(tools=["resolve_product", "get_variant"])
    assert check_tool_selection(out, {"expected_tools": ["resolve_product"]})[0]
    ok, why = check_tool_selection(out, {"expected_tools": ["get_stats"]})
    assert not ok and "get_stats" in why
    ok, why = check_tool_selection(out, {"expected_tools": [], "forbidden_tools": ["get_variant"]})
    assert not ok and "forbidden" in why and "get_variant" in why


# Layer 4 — price hallucination
def test_extract_prices_finds_currency_and_decimal_amounts_only():
    text = "₹175.00 or $12.50, also 339.00 flat. 172 pages, 0.5mm tip, Rs 145"
    assert extract_prices(text) == [175.0, 12.5, 339.0, 145.0]

def test_price_hallucination_flags_price_not_in_allowed_set():
    ok, why = check_price_hallucination(_out(reply="It costs ₹199.00"), {"allowed_prices": [175.0]})
    assert not ok and "199" in why
    assert check_price_hallucination(_out(reply="It costs ₹175.00"), {"allowed_prices": [175.0]})[0]
    # no prices mentioned -> clean
    ok, why = check_price_hallucination(_out(reply="I could not find that product."), {"allowed_prices": []})
    assert ok and "no prices" in why


# Layer 5 — business logic
def test_no_claim_applied_rule():
    meta = {"rules": ["no_claim_applied"]}
    ok, why = check_business_logic(_out(reply="I have enabled dynamic pricing for you."), meta)
    assert not ok and "claims" in why
    assert check_business_logic(_out(reply="Here is a preview — press Continue to enable."), meta)[0]

def test_must_ask_when_ambiguous_rule():
    meta = {"rules": ["must_ask_when_ambiguous"]}
    assert check_business_logic(_out(tools=["resolve_product", "ask_user"]), meta)[0]
    ok, why = check_business_logic(_out(tools=["resolve_product", "preview_price_change"]), meta)
    assert not ok and "ask_user" in why


def test_price_change_needs_preview_rule():
    meta = {"rules": ["price_change_needs_preview"]}
    assert check_business_logic(_out(tools=["resolve_product", "preview_price_change"]), meta)[0]
    assert not check_business_logic(_out(tools=["resolve_product"]), meta)[0]


def test_unknown_rule_raises():
    with pytest.raises(ValueError, match="unknown business-logic rule"):
        check_business_logic(_out(), {"rules": ["toggle_needs_previw"]})


def test_evaluation_names_match_report_layers():
    names = {
        OutputCorrectness().get_default_evaluation_name(),
        StructuredOutput().get_default_evaluation_name(),
        ToolSelection().get_default_evaluation_name(),
        PriceHallucination().get_default_evaluation_name(),
        BusinessLogic().get_default_evaluation_name(),
        ToolRecall().get_default_evaluation_name(),
        ToolPrecision().get_default_evaluation_name(),
        ToolSuccess().get_default_evaluation_name(),
    }
    assert names == set(ALL_LAYERS)


def test_judge_rubric_removed():
    assert not hasattr(ev_module, "JUDGE_RUBRIC")
