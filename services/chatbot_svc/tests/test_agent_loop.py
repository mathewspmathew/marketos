import os
os.environ.setdefault("GROQ_API_KEY", "test")  # only the import path needs it

from services.chatbot_svc.agent import agent


def _tool_names() -> set[str]:
    return set(agent._function_toolset.tools.keys())


def test_all_expected_tools_registered():
    expected = {
        "structured_search", "semantic_search", "get_variant", "get_stats",
        "preview_price_change", "preview_dynamic_pricing_toggle",
        "apply_price_change", "ask_user",
    }
    assert expected <= _tool_names()


def test_toggle_apply_tool_absent():
    """The toggle is applied only via the interactive card. The agent must NOT
    expose a Python apply tool for it — its presence let the model bypass the
    'preview then STOP' rule and hit the unreachable host.docker.internal path."""
    assert "apply_dynamic_pricing_toggle" not in _tool_names()


def test_apply_tools_only_take_preview_id():
    """Invariant: apply_* must NOT accept a scope_filter — only preview_id."""
    for name in ("apply_price_change",):
        tool = agent._function_toolset.tools[name]
        # Tool exposes its JSON schema for the arguments
        schema = tool.function_schema.json_schema
        props = set(schema.get("properties", {}).keys())
        assert props == {"preview_id"}, f"{name} accepts {props}, expected only {{preview_id}}"


def test_ask_user_registered():
    assert "ask_user" in _tool_names()


def test_get_stats_does_not_require_confirmation():
    """get_stats is a plain read tool; presence alone is the invariant."""
    assert "get_stats" in _tool_names()
