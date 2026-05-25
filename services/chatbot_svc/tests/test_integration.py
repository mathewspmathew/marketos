import json, uuid
from datetime import datetime, timezone
import pytest
import respx
from httpx import Response, AsyncClient

from services.common.db import get_db
from services.common.models import ChatSession
from services.chatbot_svc.tools.preview import (
    preview_price_change, preview_dynamic_pricing_toggle,
)
from services.chatbot_svc.tools.apply import (
    apply_price_change, apply_dynamic_pricing_toggle,
)
from services.chatbot_svc.tools.stats import get_stats, StatsMetric
from services.chatbot_svc.schemas import ScopeFilter, PriceChange
from services.chatbot_svc.deps import AgentDeps


@pytest.fixture
def session_row(seed_shop):
    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=sid, shopDomain=seed_shop, createdAt=now, updatedAt=now))
    yield sid
    with get_db() as s:
        row = s.get(ChatSession, sid)
        if row:
            s.delete(row)


def _deps(shop, sid):
    return AgentDeps(
        shop_domain=shop, user_id=None, session_id=sid,
        rr_base_url="http://rr.test", internal_token="tok",
        http=AsyncClient(),
    )


@pytest.mark.asyncio
async def test_full_price_change_flow(seed_shop, session_row):
    """(b) Price change: preview → apply round-trip with stubbed RR."""
    prev = preview_price_change(
        seed_shop, session_row,
        ScopeFilter(vendor="Boat"),
        PriceChange(type="percent", value=10),
    )
    assert prev.count >= 1
    async with respx.mock(base_url="http://rr.test") as r:
        succeeded_ids = [row.variant_id for row in prev.sample_rows]
        r.post("/internal/apply-chat-price").mock(
            return_value=Response(
                200,
                json={"ok": True, "preview_id": prev.preview_id,
                      "succeeded": succeeded_ids, "failed": []},
            )
        )
        deps = _deps(seed_shop, session_row)
        res = await apply_price_change(deps, prev.preview_id)
        await deps.http.aclose()
    assert res.preview_id == prev.preview_id
    assert res.succeeded == succeeded_ids


@pytest.mark.asyncio
async def test_full_dynamic_pricing_toggle_flow(seed_shop, session_row):
    """(a) Toggle: preview → apply for dynamic_pricing_toggle."""
    prev = preview_dynamic_pricing_toggle(
        seed_shop, session_row,
        ScopeFilter(vendor="Boat"),
        enabled=True,
    )
    assert prev.count >= 1
    async with respx.mock(base_url="http://rr.test") as r:
        succeeded_product_ids = [row.product_id for row in prev.sample_rows]
        route = r.post("/internal/apply-chat-flag").mock(
            return_value=Response(
                200,
                json={"ok": True, "preview_id": prev.preview_id,
                      "succeeded": succeeded_product_ids, "failed": []},
            )
        )
        deps = _deps(seed_shop, session_row)
        res = await apply_dynamic_pricing_toggle(deps, prev.preview_id)
        await deps.http.aclose()
    assert res.preview_id == prev.preview_id
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["preview_id"] == prev.preview_id


def test_intent_c_stats_no_confirm_required(seed_shop):
    """(c) Stats: get_stats answers directly, no preview gate."""
    out = get_stats(seed_shop, StatsMetric.match_coverage)
    assert "matched" in out and "total" in out
    assert isinstance(out["total"], int)
