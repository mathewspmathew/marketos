import os
os.environ.setdefault("GROQ_API_KEY", "test")  # only the import path needs it

from services.chatbot_svc.agent import agent


def _tool_names() -> set[str]:
    return set(agent._function_toolset.tools.keys())


def test_all_expected_tools_registered():
    expected = {
        "structured_search", "semantic_search", "get_variant", "get_stats",
        "preview_price_change", "open_dynamic_pricing_panel", "ask_user",
    }
    assert expected <= _tool_names()


def test_toggle_preview_tool_removed():
    """The scope+enabled toggle preview is retired; the panel replaces it."""
    assert "preview_dynamic_pricing_toggle" not in _tool_names()


def test_apply_tools_absent():
    """The agent has NO apply tools — price changes and dynamic-pricing toggles
    are applied only via their interactive cards (the browser path), never by the
    agent hitting the unreachable host.docker.internal RR route."""
    names = _tool_names()
    assert "apply_price_change" not in names
    assert "apply_dynamic_pricing_toggle" not in names


def test_ask_user_registered():
    assert "ask_user" in _tool_names()


def test_get_stats_does_not_require_confirmation():
    """get_stats is a plain read tool; presence alone is the invariant."""
    assert "get_stats" in _tool_names()


def test_resolve_product_tool_registered():
    from services.chatbot_svc.agent import agent
    assert "resolve_product" in list(agent._function_toolset.tools)


def test_get_dynamic_pricing_status_tool_registered():
    from services.chatbot_svc.agent import agent
    assert "get_dynamic_pricing_status" in list(agent._function_toolset.tools)
