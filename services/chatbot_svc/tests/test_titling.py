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
