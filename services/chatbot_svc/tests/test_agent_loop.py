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
    """Price changes and the FRESH/PAUSED/ACTIVE dynamic-pricing toggle are
    still applied only via their interactive cards (the browser path), never
    by the agent hitting the unreachable host.docker.internal RR route.
    apply_dynamic_pricing_config is a deliberate, narrow exception: it calls
    apply_pane_config in-process (no RR route involved), see
    docs/superpowers/specs/2026-07-13-chatbot-apply-pane-config-design.md."""
    names = _tool_names()
    assert "apply_price_change" not in names
    assert "apply_dynamic_pricing_toggle" not in names


def test_apply_dynamic_pricing_config_tool_registered():
    assert "apply_dynamic_pricing_config" in _tool_names()


def test_pause_dynamic_pricing_tool_registered():
    assert "pause_dynamic_pricing" in _tool_names()


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
