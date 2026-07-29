"""services/api_gateway/tests/test_live_updates_endpoints.py

Confirms both /internal/dynamic-pricing/{matches,stats}/stream routes are
wired: reachable only with the internal token, and connecting registers a
live_updates subscriber for the requested (channel, shop_domain). Full event
delivery is exercised end-to-end by live_updates.py's own integration tests
(Task 3) — this test is about the HTTP wiring, not re-testing pub/sub delivery.

Both routes hold their SSE generator open forever (`while True: await
queue.get()`) until the client disconnects. Starlette's synchronous
TestClient can't exercise that directly: `_TestClientTransport.handle_request`
(starlette/testclient.py) — and httpx.ASGITransport.handle_async_request,
same story — fully drains the ASGI app coroutine before handing back a
response, so `with client.stream(...) as resp: ...` blocks forever against a
route that never finishes on its own (confirmed empirically: it hangs
indefinitely instead of returning once headers are sent). Driving the
request as a cancellable asyncio task instead lets us observe the mid-stream
subscription and then simulate a disconnect via task cancellation.
"""
import asyncio
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from services.api_gateway import live_updates
from services.api_gateway.main import app

_client = TestClient(app)
_HEADERS = {"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]}


def test_matches_stream_rejects_without_internal_token():
    resp = _client.get("/internal/dynamic-pricing/matches/stream?shop_domain=x.myshopify.com")
    assert resp.status_code == 403


def test_stats_stream_rejects_without_internal_token():
    resp = _client.get("/internal/dynamic-pricing/stats/stream?shop_domain=x.myshopify.com")
    assert resp.status_code == 403


async def _assert_subscribe_then_disconnect_unsubscribes(path: str, channel: str, shop: str) -> None:
    key = (channel, shop)
    assert key not in live_updates._subscribers

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        task = asyncio.ensure_future(client.get(f"{path}?shop_domain={shop}", headers=_HEADERS))
        try:
            for _ in range(500):  # up to ~5s
                if key in live_updates._subscribers:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError(f"{key} was never registered as a subscriber")

            assert key in live_updates._subscribers
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert key not in live_updates._subscribers


async def test_matches_stream_registers_a_subscriber():
    shop = "sse-matches-endpoint-test-shop.myshopify.com"
    await _assert_subscribe_then_disconnect_unsubscribes(
        "/internal/dynamic-pricing/matches/stream", "matches_channel", shop,
    )


async def test_stats_stream_registers_a_subscriber():
    shop = "sse-stats-endpoint-test-shop.myshopify.com"
    await _assert_subscribe_then_disconnect_unsubscribes(
        "/internal/dynamic-pricing/stats/stream", "stats_channel", shop,
    )
