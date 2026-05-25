import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from services.chatbot_svc.tools.apply import apply_price_change, apply_dynamic_pricing_toggle
from services.chatbot_svc.deps import AgentDeps


def _fake_deps(response, status=200):
    resp_mock = MagicMock()
    resp_mock.status_code = status
    resp_mock.json = lambda: response
    resp_mock.raise_for_status = lambda: None
    http = MagicMock()
    http.post = AsyncMock(return_value=resp_mock)
    return AgentDeps(
        shop_domain="test.myshopify.com",
        user_id="u-1",
        session_id="s-1",
        rr_base_url="http://rr.test",
        internal_token="tok",
        http=http,
    )


def test_apply_price_change_posts_to_rr():
    deps = _fake_deps({"ok": True, "preview_id": "p-1", "succeeded": ["v1"], "failed": []})
    res = asyncio.run(apply_price_change(deps, "p-1"))
    assert res.preview_id == "p-1"
    assert res.succeeded == ["v1"]
    deps.http.post.assert_awaited_once()
    args, kwargs = deps.http.post.call_args
    assert args[0] == "http://rr.test/internal/apply-chat-price"
    assert kwargs["headers"]["x-internal-token"] == "tok"
    assert kwargs["json"] == {"preview_id": "p-1", "applied_by": "u-1"}


def test_apply_flag_posts_to_rr():
    deps = _fake_deps({"ok": True, "preview_id": "p-2", "succeeded": ["prod-1"], "failed": []})
    res = asyncio.run(apply_dynamic_pricing_toggle(deps, "p-2"))
    assert res.preview_id == "p-2"
    args, _ = deps.http.post.call_args
    assert args[0] == "http://rr.test/internal/apply-chat-flag"


def test_apply_raises_on_not_ok():
    deps = _fake_deps({"ok": False, "reason": "expired"})
    with pytest.raises(RuntimeError, match="expired"):
        asyncio.run(apply_price_change(deps, "p-x"))


def test_apply_trims_trailing_slash():
    deps = _fake_deps({"ok": True, "preview_id": "p-3", "succeeded": [], "failed": []})
    deps.rr_base_url = "http://rr.test/"  # trailing slash
    asyncio.run(apply_price_change(deps, "p-3"))
    args, _ = deps.http.post.call_args
    assert args[0] == "http://rr.test/internal/apply-chat-price"
