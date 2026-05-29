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
