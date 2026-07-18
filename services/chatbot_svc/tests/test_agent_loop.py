import os
os.environ.setdefault("GROQ_API_KEY", "test")  # only the import path needs it

from services.chatbot_svc.agent import agent


def _tool_names() -> set[str]:
    return set(agent._function_toolset.tools.keys())


def test_all_expected_tools_registered():
    expected = {
        "structured_search", "semantic_search", "get_variant", "get_stats",
        "preview_price_change", "ask_user",
    }
    assert expected <= _tool_names()


def test_open_dynamic_pricing_panel_retired():
    """Commented out (not deleted) in agent.py — direct tools (apply/pause/
    delete) now cover every dynamic-pricing chat action, including resume
    (apply_dynamic_pricing_config with every field omitted) and ambiguous
    requests (get_dynamic_pricing_status). See prompts/system.md's Hard rule
    decision procedure."""
    assert "open_dynamic_pricing_panel" not in _tool_names()


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


def test_resume_dynamic_pricing_tool_registered():
    assert "resume_dynamic_pricing" in _tool_names()


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


def test_get_delete_preview_tool_registered():
    assert "get_delete_preview" in _tool_names()


def test_delete_dynamic_pricing_tool_registered():
    assert "delete_dynamic_pricing" in _tool_names()


def test_system_prompt_requires_explicit_tier_options():
    """Regression guard for a live-verified-but-prompt-only behavior: the
    missing-tier ask_user call must pass explicit BUDGET/COMPETITIVE/PREMIUM
    options, not an open-ended question. There is no code-level way to force
    the LLM's actual ask_user call, so this pins the instruction text itself
    — if a future system.md edit silently drops this requirement, this test
    fails and signals "re-run the live disambiguation/tier-options test
    before trusting this again" (see
    docs/superpowers/plans/2026-07-14-disambiguation-tests-and-tier-options.md)."""
    from services.chatbot_svc.agent import _PROMPT
    assert 'options=["BUDGET", "COMPETITIVE", "PREMIUM"]' in _PROMPT


def test_system_prompt_requires_titles_as_multi_match_options():
    """Regression guard for the multi-match disambiguation instruction —
    same rationale as test_system_prompt_requires_explicit_tier_options."""
    from services.chatbot_svc.agent import _PROMPT
    assert "candidates' titles as the `options` list" in _PROMPT


def test_apply_dynamic_pricing_config_docstring_requires_tier_options():
    """Same regression guard, on the tool docstring surface (agent.py),
    which the LLM also reads directly from the tool's own definition."""
    from services.chatbot_svc.agent import agent
    tool = agent._function_toolset.tools["apply_dynamic_pricing_config"]
    docstring = tool.function.__doc__ or ""
    assert "ask_user's options MUST be" in docstring
    assert '["BUDGET", "COMPETITIVE", "PREMIUM"]' in docstring


def test_resolve_product_tool_records_results_to_session(monkeypatch):
    import uuid
    from datetime import datetime, timezone
    from services.common.db import get_db
    from services.common.models import ChatSession, ShopifyUser
    from services.chatbot_svc import agent as agent_module
    from services.chatbot_svc.schemas import ResolvedProduct
    from services.chatbot_svc.deps import AgentDeps

    shop = f"resolve-record-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    session_id = uuid.uuid4().hex

    with get_db() as s:
        s.add(ShopifyUser(shopDomain=shop))
        s.flush()
        now = datetime.now(timezone.utc)
        s.add(ChatSession(id=session_id, shopDomain=shop, createdAt=now, updatedAt=now))

    try:
        fake_result = [ResolvedProduct(
            product_id="gid://shopify/Product/abc", title="Test Product",
            variant_ids=["gid://shopify/ProductVariant/1"], dynamic_pricing_enabled=False,
        )]
        monkeypatch.setattr(agent_module.t_search, "resolve_product", lambda *a, **k: fake_result)

        deps = AgentDeps(
            shop_domain=shop, user_id=None, session_id=session_id,
            rr_base_url="http://test", internal_token="test", http=None,
        )

        # Call the underlying function directly (agent.tool wraps it, but the
        # plain function object is reachable via .function on the Tool).
        tool = agent_module.agent._function_toolset.tools["resolve_product"]
        result = tool.function(_FakeCtx(deps), "Test Product")
        assert result == fake_result

        with get_db() as s:
            session = s.get(ChatSession, session_id)
            assert "gid://shopify/Product/abc" in session.resolvedProductIds
    finally:
        with get_db() as s:
            s.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)
            s.query(ShopifyUser).filter(ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


class _FakeCtx:
    """Minimal stand-in for pydantic_ai's RunContext — the tool function
    only reads ctx.deps."""
    def __init__(self, deps):
        self.deps = deps
