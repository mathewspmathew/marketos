"""services/api_gateway/live_updates.py

In-memory pub/sub for the live-updating Matches and Stats pages. A single
background asyncio task (run_listener, started from main.py's FastAPI
lifespan) holds one dedicated async psycopg connection LISTENing on both
matches_channel (services/matcher_svc/main.py notifies it) and stats_channel
(services/pricing_svc/decide.py notifies it), and fans each NOTIFY out to
every asyncio.Queue subscribed for that (channel, shop_domain) pair.
subscribe/unsubscribe are called once per open SSE connection in main.py's
two /internal/dynamic-pricing/{matches,stats}/stream routes.

One shared connection for both channels, not one each — Aiven's connection
ceiling is already tight (see services/common/db.py's pool-sizing comments),
and there is no benefit to two held-open connections over one LISTENing on
two channel names.
"""
import asyncio

import psycopg
import structlog

logger = structlog.get_logger(__name__)

_subscribers: dict[tuple[str, str], set[asyncio.Queue]] = {}

_INITIAL_BACKOFF_SECONDS = 1
_MAX_BACKOFF_SECONDS = 30


def subscribe(channel: str, shop_domain: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault((channel, shop_domain), set()).add(queue)
    return queue


def unsubscribe(channel: str, shop_domain: str, queue: asyncio.Queue) -> None:
    key = (channel, shop_domain)
    queues = _subscribers.get(key)
    if not queues:
        return
    queues.discard(queue)
    if not queues:
        del _subscribers[key]


def _dispatch(channel: str, shop_domain: str, value=None) -> None:
    for queue in _subscribers.get((channel, shop_domain), ()):
        # put_nowait: a full queue means the SSE route hasn't drained an
        # earlier wake-up yet, so this shop already has one pending — no
        # need to queue a second one to know "something changed."
        if queue.empty():
            queue.put_nowait(value)


async def run_listener(dsn: str, *, stop_event: asyncio.Event) -> None:
    """Runs until stop_event is set. Reconnects with exponential backoff
    (capped at _MAX_BACKOFF_SECONDS) on any connection error — a dropped
    DB connection must not kill the whole api_gateway process, it should
    just mean live updates are briefly unavailable until reconnected."""
    backoff = _INITIAL_BACKOFF_SECONDS
    while not stop_event.is_set():
        try:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                await conn.execute("LISTEN matches_channel")
                await conn.execute("LISTEN stats_channel")
                logger.info("live_updates_listener_connected")
                backoff = _INITIAL_BACKOFF_SECONDS
                # notifies() is given a short timeout so its internal wait loop
                # returns control here periodically even when nothing arrives —
                # otherwise a quiet connection would block __anext__ forever and
                # stop_event would never get re-checked, hanging shutdown.
                while not stop_event.is_set():
                    async for notify in conn.notifies(timeout=1):
                        if stop_event.is_set():
                            break
                        if notify.channel == "matches_channel":
                            _dispatch("matches_channel", notify.payload)
                        elif notify.channel == "stats_channel":
                            shop_domain, _, product_id = notify.payload.partition(":")
                            _dispatch("stats_channel", shop_domain, value=product_id)
        except Exception:
            if stop_event.is_set():
                break
            logger.exception("live_updates_listener_disconnected", retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
