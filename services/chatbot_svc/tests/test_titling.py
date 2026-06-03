from services.chatbot_svc.titling import is_refusal, REFUSAL_SENTENCE


def test_is_refusal_exact():
    assert is_refusal(REFUSAL_SENTENCE) is True


def test_is_refusal_trailing_whitespace_and_period():
    assert is_refusal("  I can only help with your store's products and pricing.  ") is True


def test_is_refusal_real_answer_is_false():
    assert is_refusal("Your cheapest variant is **$9.99**.") is False


def test_is_refusal_empty_is_false():
    assert is_refusal("") is False
    assert is_refusal(None) is False


import pytest
from services.chatbot_svc.titling import clean_title


def test_clean_title_strips_quotes_and_period():
    assert clean_title('"Nike price discount."') == "Nike price discount"


def test_clean_title_truncates_to_60_chars():
    long = "word " * 40
    assert len(clean_title(long)) <= 60


def test_clean_title_collapses_whitespace_and_newlines():
    assert clean_title("Nike\n  discount\tplan") == "Nike discount plan"


def test_clean_title_empty():
    assert clean_title("") == ""
    assert clean_title("   ") == ""
