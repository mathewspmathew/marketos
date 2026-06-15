from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from services.chatbot_svc.evals.runner import count_retries, extract_tool_calls


def _messages():
    return [
        ModelRequest(parts=[UserPromptPart(content="price of the pen?")]),
        ModelResponse(parts=[ToolCallPart(tool_name="resolve_product", args={"reference": "pen"}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="resolve_product", content=[], tool_call_id="c1")]),
        ModelResponse(parts=[ToolCallPart(tool_name="get_variant", args='{"variant_id": "v1"}', tool_call_id="c2")]),
        ModelRequest(parts=[RetryPromptPart(content="bad args", tool_name="get_variant", tool_call_id="c2")]),
        ModelResponse(parts=[TextPart(content="The pen costs 175.00")]),
    ]


def test_extract_tool_calls_names_and_args_in_order():
    calls = extract_tool_calls(_messages())
    assert [c["tool_name"] for c in calls] == ["resolve_product", "get_variant"]
    assert calls[0]["args"] == {"reference": "pen"}
    # string args (some providers send JSON strings) are parsed to dicts
    assert calls[1]["args"] == {"variant_id": "v1"}


def test_count_retries():
    assert count_retries(_messages()) == 1
    assert count_retries([]) == 0


def test_extract_tool_calls_unparseable_string_args_kept_raw():
    msgs = [ModelResponse(parts=[ToolCallPart(tool_name="get_stats", args="not-json", tool_call_id="c3")])]
    assert extract_tool_calls(msgs) == [{"tool_name": "get_stats", "args": {"_raw": "not-json"}}]
