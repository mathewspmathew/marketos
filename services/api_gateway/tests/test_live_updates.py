"""services/api_gateway/tests/test_live_updates.py

Unit tests for the in-memory pub/sub fan-out (subscribe/unsubscribe/_dispatch)
plus one integration test per channel proving a real Postgres NOTIFY reaches
a subscribed queue via run_listener. The pub/sub primitives are kept free of
any live DB connection so they're testable fast and deterministically;
run_listener is the one piece that needs a real connection.
"""
import asyncio
import os

import psycopg
import pytest

from services.api_gateway import live_updates


def test_subscribe_returns_a_queue_and_unsubscribe_removes_it():
    q = live_updates.subscribe("matches_channel", "shop-a.myshopify.com")
    assert isinstance(q, asyncio.Queue)
    assert q in live_updates._subscribers[("matches_channel", "shop-a.myshopify.com")]

    live_updates.unsubscribe("matches_channel", "shop-a.myshopify.com", q)
    assert ("matches_channel", "shop-a.myshopify.com") not in live_updates._subscribers


def test_dispatch_only_wakes_matching_channel_and_shop():
    q_matches_a = live_updates.subscribe("matches_channel", "shop-a.myshopify.com")
    q_stats_a = live_updates.subscribe("stats_channel", "shop-a.myshopify.com")
    q_matches_b = live_updates.subscribe("matches_channel", "shop-b.myshopify.com")

    live_updates._dispatch("matches_channel", "shop-a.myshopify.com")

    assert q_matches_a.qsize() == 1
    assert q_stats_a.qsize() == 0
    assert q_matches_b.qsize() == 0

    live_updates.unsubscribe("matches_channel", "shop-a.myshopify.com", q_matches_a)
    live_updates.unsubscribe("stats_channel", "shop-a.myshopify.com", q_stats_a)
    live_updates.unsubscribe("matches_channel", "shop-b.myshopify.com", q_matches_b)


def test_dispatch_carries_a_value_for_stats_channel():
    q = live_updates.subscribe("stats_channel", "shop-a.myshopify.com")
    live_updates._dispatch("stats_channel", "shop-a.myshopify.com", value="gid://shopify/Product/1")
    assert q.get_nowait() == "gid://shopify/Product/1"
    live_updates.unsubscribe("stats_channel", "shop-a.myshopify.com", q)


def test_dispatch_with_no_subscribers_is_a_noop():
    live_updates._dispatch("matches_channel", "nobody-listening.myshopify.com")  # must not raise


def _raw_dsn():
    url = os.environ["DATABASE_URL"]
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


@pytest.mark.asyncio
async def test_run_listener_delivers_a_real_matches_notify_to_a_subscriber():
    stop_event = asyncio.Event()
    listener_task = asyncio.create_task(live_updates.run_listener(_raw_dsn(), stop_event=stop_event))
    await asyncio.sleep(1.5)  # let the listener finish LISTEN before we NOTIFY

    q = live_updates.subscribe("matches_channel", "integration-test-shop.myshopify.com")
    try:
        with psycopg.connect(_raw_dsn(), autocommit=True) as notify_conn:
            notify_conn.execute(
                "SELECT pg_notify('matches_channel', %s)",
                ("integration-test-shop.myshopify.com",),
            )
        await asyncio.wait_for(q.get(), timeout=5)
    finally:
        live_updates.unsubscribe("matches_channel", "integration-test-shop.myshopify.com", q)
        stop_event.set()
        await asyncio.wait_for(listener_task, timeout=5)


@pytest.mark.asyncio
async def test_run_listener_delivers_a_real_stats_notify_with_product_id():
    stop_event = asyncio.Event()
    listener_task = asyncio.create_task(live_updates.run_listener(_raw_dsn(), stop_event=stop_event))
    await asyncio.sleep(1.5)

    q = live_updates.subscribe("stats_channel", "integration-test-shop.myshopify.com")
    try:
        with psycopg.connect(_raw_dsn(), autocommit=True) as notify_conn:
            notify_conn.execute(
                "SELECT pg_notify('stats_channel', %s)",
                ("integration-test-shop.myshopify.com:gid://shopify/Product/1",),
            )
        product_id = await asyncio.wait_for(q.get(), timeout=5)
        assert product_id == "gid://shopify/Product/1"
    finally:
        live_updates.unsubscribe("stats_channel", "integration-test-shop.myshopify.com", q)
        stop_event.set()
        await asyncio.wait_for(listener_task, timeout=5)
