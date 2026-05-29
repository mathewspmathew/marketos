"""Unit tests for services.chatbot_svc.context."""
from services.chatbot_svc.context import count_tokens


def test_count_tokens_empty_string():
    assert count_tokens("") == 0


def test_count_tokens_short_text():
    # "hello world" = 11 chars; heuristic = 11 // 4 = 2, then max(1, ...) = 2
    assert count_tokens("hello world") == 2


def test_count_tokens_dict_payload():
    # Dicts must be serialised before counting.
    payload = {"text": "x" * 400}
    n = count_tokens(payload)
    # 400 chars + JSON overhead -> >=100 tokens, <=120
    assert 100 <= n <= 120


def test_count_tokens_minimum_is_one_for_nonempty():
    assert count_tokens("a") == 1
    assert count_tokens("ab") == 1


from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from services.chatbot_svc.context import row_to_model_message


def test_user_row_becomes_model_request():
    msg = row_to_model_message("user", {"text": "hello"})
    assert isinstance(msg, ModelRequest)
    assert isinstance(msg.parts[0], UserPromptPart)
    assert msg.parts[0].content == "hello"


def test_assistant_row_becomes_model_response():
    msg = row_to_model_message("assistant", {"text": "hi there"})
    assert isinstance(msg, ModelResponse)
    assert isinstance(msg.parts[0], TextPart)
    assert msg.parts[0].content == "hi there"


def test_assistant_ask_row_renders_question_text():
    # When the assistant raised ask_user, content has {"ask": {"question": "...", "options": [...]}}.
    msg = row_to_model_message(
        "assistant",
        {"ask": {"question": "Which vendor?", "options": ["Boat", "JBL"]}},
    )
    assert isinstance(msg, ModelResponse)
    assert "Which vendor?" in msg.parts[0].content


def test_tool_row_returns_none():
    msg = row_to_model_message("tool", {"tool_name": "apply", "tool_result": {"ok": True}})
    assert msg is None


def test_user_row_with_missing_text_returns_none():
    assert row_to_model_message("user", {}) is None
