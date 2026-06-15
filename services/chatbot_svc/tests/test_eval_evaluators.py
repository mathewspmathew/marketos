import pytest

from services.chatbot_svc.evals.evaluators import (
    check_business_logic,
    check_hallucination,
    check_output_correctness,
    check_structured_output,
    check_tool_selection,
    extract_prices,
)
from services.chatbot_svc.evals.runner import ChatRunOutput


def _out(reply="", tools=(), retries=0, error=None):
    return ChatRunOutput(
        reply=reply,
        tool_calls=[{"tool_name": t, "args": {}} for t in tools],
        retries=retries,
        error=error,
    )


# Layer 1 — output correctness
def test_correctness_passes_when_all_facts_present():
    out = _out(reply="The Pilot V5 pen costs ₹175.00.")
    assert check_output_correctness(out, {"expected_facts": ["Pilot", "175"]})

def test_correctness_fails_on_missing_fact_and_is_case_insensitive():
    assert not check_output_correctness(_out(reply="It costs 175"), {"expected_facts": ["pilot"]})
    assert check_output_correctness(_out(reply="the PILOT pen"), {"expected_facts": ["Pilot"]})


# Layer 2 — structured output
def test_structured_output_fails_on_retries_or_error():
    assert check_structured_output(_out(), {})
    assert not check_structured_output(_out(retries=1), {})
    assert not check_structured_output(_out(error="ValidationError"), {})


# Layer 3 — tool selection
def test_tool_selection_requires_expected_and_blocks_forbidden():
    out = _out(tools=["resolve_product", "get_variant"])
    assert check_tool_selection(out, {"expected_tools": ["resolve_product"]})
    assert not check_tool_selection(out, {"expected_tools": ["get_stats"]})
    assert not check_tool_selection(out, {"expected_tools": [], "forbidden_tools": ["get_variant"]})


# Layer 4 — hallucination
def test_extract_prices_finds_currency_and_decimal_amounts_only():
    text = "₹175.00 or $12.50, also 339.00 flat. 172 pages, 0.5mm tip, Rs 145"
    assert extract_prices(text) == [175.0, 12.5, 339.0, 145.0]

def test_hallucination_flags_price_not_in_allowed_set():
    out = _out(reply="It costs ₹199.00")
    assert not check_hallucination(out, {"allowed_prices": [175.0]})
    assert check_hallucination(_out(reply="It costs ₹175.00"), {"allowed_prices": [175.0]})
    # no prices mentioned -> clean
    assert check_hallucination(_out(reply="I could not find that product."), {"allowed_prices": []})


# Layer 5 — business logic
def test_toggle_needs_preview_rule():
    meta = {"rules": ["toggle_needs_preview"]}
    assert check_business_logic(_out(tools=["resolve_product", "preview_dynamic_pricing_toggle"]), meta)
    assert not check_business_logic(_out(tools=["resolve_product"]), meta)

def test_no_claim_applied_rule():
    meta = {"rules": ["no_claim_applied"]}
    assert not check_business_logic(_out(reply="I have enabled dynamic pricing for you."), meta)
    assert check_business_logic(_out(reply="Here is a preview — press Continue to enable."), meta)

def test_must_ask_when_ambiguous_rule():
    meta = {"rules": ["must_ask_when_ambiguous"]}
    assert check_business_logic(_out(tools=["resolve_product", "ask_user"]), meta)
    assert not check_business_logic(_out(tools=["resolve_product", "preview_price_change"]), meta)


def test_price_change_needs_preview_rule():
    meta = {"rules": ["price_change_needs_preview"]}
    assert check_business_logic(_out(tools=["resolve_product", "preview_price_change"]), meta)
    assert not check_business_logic(_out(tools=["resolve_product"]), meta)


def test_unknown_rule_raises():
    with pytest.raises(ValueError, match="unknown business-logic rule"):
        check_business_logic(_out(), {"rules": ["toggle_needs_previw"]})
