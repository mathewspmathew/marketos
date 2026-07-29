"""
services/api_gateway/main.py

Internal HTTP API consumed by the Shopify frontend (React Router webhooks).
Not exposed publicly — only reachable within the Docker network.

Run: uvicorn services.api_gateway.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import or_, text
from sse_starlette.sse import EventSourceResponse

from services.api_gateway import live_updates
from services.chatbot_svc.schemas import PaneConfigInput
from services.chatbot_svc.tools.toggle_settings import compute_disable_counts
from services.common import pane_config
from services.common.celery_app import app as celery_app
from services.common.db import get_db
from services.common.frequency import next_run_at
from services.common.models import ProductUrl, ShopifyProduct
from services.pricing_svc import product_stats
from services.pricing_svc.decide import get_skip_reason_taxonomy
from services.pricing_svc.revert import RevertError, revert_price_decision
from services.pricing_svc.match_review import MatchReviewError, confirm_match, reject_match
from services.scraper_svc.semantics import claim_and_enqueue_semantics

load_dotenv()

logger = structlog.get_logger(__name__)

_live_updates_stop = asyncio.Event()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    dsn = os.environ["DATABASE_URL"]
    listener_task = asyncio.create_task(live_updates.run_listener(dsn, stop_event=_live_updates_stop))
    yield
    _live_updates_stop.set()
    await listener_task


app = FastAPI(title="MarketOS Internal API", docs_url=None, redoc_url=None, lifespan=_lifespan)


@app.middleware("http")
async def require_internal_token(request: Request, call_next):
    # Every /internal/* route trusts its inputs (shop_domain, product_id, ...)
    # with no further authorization check, so this gateway must never be
    # reachable without the shared secret — same X-Internal-Token pattern
    # used by services/chatbot_svc/app.py and the frontend's internal.*
    # proxy routes. /health is intentionally exempt for container healthchecks.
    if request.url.path.startswith("/internal/"):
        expected = os.environ.get("INTERNAL_API_TOKEN", "")
        if not expected or request.headers.get("x-internal-token") != expected:
            return JSONResponse(status_code=403, content={"ok": False, "error": "Forbidden"})
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # A malformed request body/query never reaches a route's own try/except —
    # FastAPI raises this before the route runs. Without this handler it's
    # invisible in the logs and returns FastAPI's default {"detail": [...]}
    # shape, which every JS caller's `data.ok ? ... : data.error` renders as
    # a blank error message.
    logger.warning("request_validation_failed", path=request.url.path, errors=exc.errors())
    return JSONResponse(status_code=422, content={"ok": False, "error": "Invalid request."})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Last-resort net for anything that escapes every route's own
    # `except Exception` — e.g. a failure before the route body runs.
    logger.exception("unhandled_exception", path=request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": "Something went wrong."})


@app.post("/internal/shopify/product-updated")
def shopify_product_updated(product_id: str):
    """Triggered by products/create / products/update webhooks after DB upsert.
    Claims the product (PENDING -> QUEUED) and enqueues one semantic task."""
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id is required")
    try:
        with get_db() as session:
            claim_and_enqueue_semantics(session, ids=[product_id])
    except Exception:
        logger.exception("shopify_product_updated_failed", product_id=product_id)
        raise HTTPException(status_code=500, detail="failed to queue semantics")
    return {"queued": True, "product_id": product_id}


@app.post("/internal/shopify/retry-failed-semantics")
def retry_failed_semantics(shop_domain: str):
    """Reset all FAILED products for a shop back to PENDING (+ bump version) so the
    next beat tick re-claims them."""
    if not shop_domain:
        raise HTTPException(status_code=422, detail="shop_domain is required")
    try:
        with get_db() as session:
            rc = session.execute(text('''
                UPDATE "ShopifyProduct"
                   SET "semanticStatus"='PENDING', "semanticVersion"="semanticVersion"+1,
                       "semanticFailureReason"=NULL
                 WHERE "shopDomain"=:sd AND "semanticStatus"='FAILED'
            '''), {"sd": shop_domain}).rowcount
            session.commit()
    except Exception:
        logger.exception("retry_failed_semantics_failed", shop_domain=shop_domain)
        raise HTTPException(status_code=500, detail="failed to reset semantics")
    return {"ok": True, "reset": rc}


@app.post("/internal/shopify/backfill-semantics")
def backfill_shopify_semantics():
    """Manual kick of the semantic backfill — same bounded claim the beat uses.
    Claims PENDING/stale-QUEUED products (at most the claim's batch limit) and
    enqueues one task each; never bypasses the claim."""
    try:
        with get_db() as session:
            claimed = claim_and_enqueue_semantics(session, ids=None)
    except Exception:
        logger.exception("backfill_shopify_semantics_failed")
        raise HTTPException(status_code=500, detail="failed to queue semantics backfill")
    return {"queued": len(claimed), "product_ids": claimed}


# Ported from app._index.jsx's auto-kick and app.products.jsx's Refresh-button
# debounce, which each did their own non-atomic read-then-write on ShopifyUser
# and disagreed on the rule (the auto-kick had no timeout escape hatch; the
# button did). This single atomic UPDATE is now the only place that decides
# whether a sync may start, so two near-simultaneous callers can't both win.
_CLAIM_SYNC_SLOT_SQL = text("""
    UPDATE "ShopifyUser"
    SET "productSyncState" = 'SYNCING', "productSyncStartedAt" = NOW()
    WHERE "shopDomain" = :shop
      AND (
        "productSyncState" IS DISTINCT FROM 'SYNCING'
        OR "productSyncStartedAt" IS NULL
        OR NOW() - "productSyncStartedAt" > INTERVAL '10 minutes'
      )
    RETURNING "shopDomain"
""")


@app.post("/internal/shopify/sync-products")
def shopify_sync_products(
    shop_domain: str,
    only_if_never_synced: bool = False,
    skip_if_error: bool = False,
):
    """Triggered by the products page (Refresh/Retry button) and the homepage
    loader's fresh-install auto-kick. Atomically claims a sync slot and
    enqueues a durable full product pull from Shopify into Postgres — or
    no-ops if a sync is already in flight (or, per the caller's flags, if the
    shop has already synced before / is in a persistent ERROR state)."""
    if not shop_domain:
        raise HTTPException(status_code=422, detail="shop_domain is required")

    try:
        with get_db() as session:
            if only_if_never_synced:
                row = session.execute(
                    text(
                        'SELECT "productSyncedAt", '
                        '(SELECT COUNT(*) FROM "ShopifyProduct" WHERE "shopDomain" = :shop) AS product_count '
                        'FROM "ShopifyUser" WHERE "shopDomain" = :shop'
                    ),
                    {"shop": shop_domain},
                ).first()
                if row and row.productSyncedAt is not None and row.product_count > 0:
                    return {"queued": False, "reason": "already_synced", "shop_domain": shop_domain}

            if skip_if_error:
                state_row = session.execute(
                    text('SELECT "productSyncState" FROM "ShopifyUser" WHERE "shopDomain" = :shop'),
                    {"shop": shop_domain},
                ).first()
                if state_row and state_row.productSyncState == "ERROR":
                    return {"queued": False, "reason": "error_awaits_manual_retry", "shop_domain": shop_domain}

            claimed = session.execute(_CLAIM_SYNC_SLOT_SQL, {"shop": shop_domain}).first()
            if not claimed:
                return {"queued": False, "reason": "already_syncing", "shop_domain": shop_domain}

        celery_app.send_task(
            "shopify_sync.pull_products",
            args=[shop_domain],
            queue="shopify_sync_queue",
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("pull_products_dispatch_failed", shop_domain=shop_domain)
        raise HTTPException(status_code=503, detail="failed to queue product sync")
    return {"queued": True, "shop_domain": shop_domain}


class ProductUpdateWebhookRequest(BaseModel):
    shop_domain: str
    payload: dict


@app.post("/internal/shopify/product-update-webhook")
async def product_update_webhook(req: ProductUpdateWebhookRequest):
    try:
        celery_app.send_task(
            "shopify_sync.handle_product_update",
            args=[req.shop_domain, req.payload],
            queue="shopify_sync_queue",
        )
    except Exception:
        logger.exception("product_update_webhook_dispatch_failed", shop_domain=req.shop_domain)
        return {"ok": False, "error": "failed to queue product update"}
    return {"ok": True}


class DynamicPricingApplyRequest(BaseModel):
    shop_domain: str
    product_id: str
    config: PaneConfigInput


class DynamicPricingProductRequest(BaseModel):
    shop_domain: str
    product_id: str


class DynamicPricingDeleteRequest(BaseModel):
    shop_domain: str
    product_id: str
    confirmed: bool = False


class PriceRevertRequest(BaseModel):
    shop_domain: str
    variant_id: str
    decision_id: str


class MatchReviewRequest(BaseModel):
    shop_domain: str
    match_id: str
    action: str


def _resolve_owned_product(session, shop_domain: str, product_id: str) -> ShopifyProduct:
    """Look up a ShopifyProduct and verify shop ownership. Raises ValueError
    if not found or not owned by shop_domain — endpoints convert this to a
    plain {ok: false, error} response."""
    product = session.get(ShopifyProduct, product_id)
    if product is None or product.shopDomain != shop_domain:
        raise ValueError(f"Product {product_id} not found in this shop.")
    return product


@app.post("/internal/dynamic-pricing/apply")
def dynamic_pricing_apply(req: DynamicPricingApplyRequest):
    try:
        with get_db() as s:
            product = _resolve_owned_product(s, req.shop_domain, req.product_id)
            config = pane_config.PaneConfig(
                search_query_override=req.config.search_query_override,
                pricing_tier=req.config.pricing_tier,
                min_price_override=req.config.min_price_override,
                max_price_override=req.config.max_price_override,
                frequency_unit=req.config.frequency_unit,
                frequency_interval=req.config.frequency_interval,
                discovery_num_results=req.config.discovery_num_results,
                listing_expansion_cap=req.config.listing_expansion_cap,
                clear_min_price_override=req.config.clear_min_price_override,
                clear_max_price_override=req.config.clear_max_price_override,
            )
            try:
                result = pane_config.apply_pane_config(s, product, config)
            except pane_config.PaneConfigError as exc:
                logger.warning(
                    "dynamic_pricing_http_apply_rejected",
                    shop_domain=req.shop_domain, product_id=req.product_id, error=str(exc),
                )
                return {"ok": False, "error": str(exc)}
            logger.info(
                "dynamic_pricing_http_applied",
                shop_domain=req.shop_domain, product_id=req.product_id,
                rearmed_count=result["rearmedCount"],
            )
            return {"ok": True, "rearmedCount": result["rearmedCount"]}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception(
            "dynamic_pricing_http_apply_failed",
            shop_domain=req.shop_domain, product_id=req.product_id,
        )
        return {"ok": False, "error": "Something went wrong applying dynamic pricing for this product."}


@app.post("/internal/dynamic-pricing/pause")
def dynamic_pricing_pause(req: DynamicPricingProductRequest):
    try:
        with get_db() as s:
            product = _resolve_owned_product(s, req.shop_domain, req.product_id)
            pane_config.pause_dynamic_pricing(s, product)
            logger.info("dynamic_pricing_http_paused", shop_domain=req.shop_domain, product_id=req.product_id)
            return {"ok": True}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception("dynamic_pricing_http_pause_failed", shop_domain=req.shop_domain, product_id=req.product_id)
        return {"ok": False, "error": "Something went wrong pausing dynamic pricing for this product."}


@app.post("/internal/dynamic-pricing/resume")
def dynamic_pricing_resume(req: DynamicPricingProductRequest):
    try:
        with get_db() as s:
            product = _resolve_owned_product(s, req.shop_domain, req.product_id)
            pane_config.resume_dynamic_pricing(s, product)
            logger.info("dynamic_pricing_http_resumed", shop_domain=req.shop_domain, product_id=req.product_id)
            return {"ok": True}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception("dynamic_pricing_http_resume_failed", shop_domain=req.shop_domain, product_id=req.product_id)
        return {"ok": False, "error": "Something went wrong resuming dynamic pricing for this product."}


@app.get("/internal/dynamic-pricing/delete-preview")
def dynamic_pricing_delete_preview(shop_domain: str, product_id: str):
    try:
        with get_db() as s:
            _resolve_owned_product(s, shop_domain, product_id)
            return {"ok": True, **compute_disable_counts(shop_domain, product_id)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception(
            "dynamic_pricing_http_delete_preview_failed",
            shop_domain=shop_domain, product_id=product_id,
        )
        return {"ok": False, "error": "Something went wrong previewing the delete for this product."}


@app.get("/internal/dynamic-pricing/product-stats")
def dynamic_pricing_product_stats(shop_domain: str, product_id: str):
    try:
        with get_db() as s:
            return {"ok": True, **product_stats.get_product_stats(s, shop_domain, product_id)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception(
            "dynamic_pricing_product_stats_failed",
            shop_domain=shop_domain, product_id=product_id,
        )
        return {"ok": False, "error": "Something went wrong loading stats for this product."}


@app.get("/internal/dynamic-pricing/products-stats-list")
def dynamic_pricing_products_stats_list(shop_domain: str):
    try:
        with get_db() as s:
            return {"ok": True, "products": product_stats.list_product_stats(s, shop_domain)}
    except Exception:
        logger.exception("dynamic_pricing_products_stats_list_failed", shop_domain=shop_domain)
        return {"ok": False, "error": "Something went wrong loading the stats list."}


@app.get("/internal/dynamic-pricing/match-activity")
def dynamic_pricing_match_activity(shop_domain: str, product_id: str, days: int = 30):
    try:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        with get_db() as s:
            return {"ok": True, "events": product_stats.get_match_activity(s, shop_domain, product_id, since)}
    except Exception:
        logger.exception(
            "dynamic_pricing_match_activity_failed",
            shop_domain=shop_domain, product_id=product_id,
        )
        return {"ok": False, "error": "Something went wrong loading match activity."}


@app.get("/internal/dynamic-pricing/matches")
def dynamic_pricing_matches(shop_domain: str):
    try:
        with get_db() as s:
            return {"ok": True, **product_stats.list_matches_for_review(s, shop_domain)}
    except Exception:
        logger.exception("dynamic_pricing_matches_failed", shop_domain=shop_domain)
        return {"ok": False, "error": "Something went wrong loading matches."}


@app.get("/internal/dynamic-pricing/matches/product")
def dynamic_pricing_matches_for_product(shop_domain: str, product_id: str, limit: int = 3):
    try:
        with get_db() as s:
            return {"ok": True, **product_stats.list_matches_for_product(s, shop_domain, product_id, limit)}
    except Exception:
        logger.exception(
            "dynamic_pricing_matches_for_product_failed",
            shop_domain=shop_domain, product_id=product_id,
        )
        return {"ok": False, "error": "Something went wrong loading matches for this product."}


@app.get("/internal/dynamic-pricing/matches/stream")
async def matches_stream(shop_domain: str):
    """SSE stream: emits one 'matches_updated' event whenever live_updates
    dispatches matches_channel for this shop (matcher_svc wrote a new match).
    Consumed by shopify_ui's app.matches.stream.jsx proxy, never by the
    browser directly (EventSource can't set X-Internal-Token)."""
    queue = live_updates.subscribe("matches_channel", shop_domain)

    async def event_gen():
        try:
            yield {"event": "open", "data": "{}"}
            while True:
                await queue.get()
                yield {"event": "matches_updated", "data": "{}"}
        finally:
            live_updates.unsubscribe("matches_channel", shop_domain, queue)

    return EventSourceResponse(event_gen(), ping=15)


@app.get("/internal/dynamic-pricing/stats/stream")
async def stats_stream(shop_domain: str):
    """SSE stream: emits one 'stats_updated' event (data: {"product_id": ...})
    whenever live_updates dispatches stats_channel for this shop (decide.py
    wrote a decision for one of its products). Consumed by shopify_ui's
    app.stats.stream.jsx proxy — app.stats._index.jsx reacts to any event,
    app.stats.$productId.jsx filters on product_id client-side."""
    queue = live_updates.subscribe("stats_channel", shop_domain)

    async def event_gen():
        try:
            yield {"event": "open", "data": "{}"}
            while True:
                product_id = await queue.get()
                yield {"event": "stats_updated", "data": json.dumps({"product_id": product_id})}
        finally:
            live_updates.unsubscribe("stats_channel", shop_domain, queue)

    return EventSourceResponse(event_gen(), ping=15)


@app.get("/internal/dynamic-pricing/skip-reasons")
async def dynamic_pricing_skip_reasons():
    """The full skipReason taxonomy decide.py can write, with whether each
    code means nothing shipped and merchant-facing copy. Fetched once per
    page load by the pricing catalog so it never re-derives decide.py's
    codes locally."""
    return {"ok": True, "reasons": get_skip_reason_taxonomy()}


@app.post("/internal/dynamic-pricing/delete")
def dynamic_pricing_delete(req: DynamicPricingDeleteRequest):
    if not req.confirmed:
        return {"ok": False, "error": "Deletion must be confirmed."}
    try:
        with get_db() as s:
            product = _resolve_owned_product(s, req.shop_domain, req.product_id)
            result = pane_config.delete_dynamic_pricing(s, product)
            logger.info(
                "dynamic_pricing_http_deleted",
                shop_domain=req.shop_domain, product_id=req.product_id,
                deleted_scraped_products=result["deletedScrapedProducts"],
            )
            return {"ok": True, "deletedScrapedProducts": result["deletedScrapedProducts"]}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception("dynamic_pricing_http_delete_failed", shop_domain=req.shop_domain, product_id=req.product_id)
        return {"ok": False, "error": "Something went wrong deleting dynamic-pricing data for this product."}


@app.post("/internal/dynamic-pricing/rearm-shop")
def rearm_shop_product_urls(shop_domain: str, frequency_interval: int, frequency_unit: str):
    """Triggered when a shop's global auto-rescrape switch flips OFF -> ON
    (app.settings.jsx). Re-arms every stale ACTIVE ProductUrl belonging to a
    product that's still opted into dynamic pricing with a real cadence, so
    the rescrape loop resumes on the next beat tick — mirrors the per-product
    re-arm logic in services.common.pane_config, generalized to a whole shop."""
    if not shop_domain:
        raise HTTPException(status_code=422, detail="shop_domain is required")
    try:
        next_run = next_run_at(frequency_interval, frequency_unit) or datetime.now(timezone.utc)
        with get_db() as s:
            eligible_product_ids = s.query(ShopifyProduct.id).filter(
                ShopifyProduct.shopDomain == shop_domain,
                ShopifyProduct.dynamicPricingEnabled.is_(True),
                ShopifyProduct.frequencyUnit != "never",
            )
            rearmed = (
                s.query(ProductUrl)
                .filter(
                    ProductUrl.shopDomain == shop_domain,
                    ProductUrl.status == "ACTIVE",
                    ProductUrl.shopifyProductId.in_(eligible_product_ids),
                    or_(ProductUrl.nextRunAt.is_(None), ProductUrl.nextRunAt <= datetime.now(timezone.utc)),
                )
                .update({"nextRunAt": next_run}, synchronize_session=False)
            )
    except Exception:
        logger.exception("rearm_shop_product_urls_failed", shop_domain=shop_domain)
        raise HTTPException(status_code=500, detail="failed to re-arm rescrape schedules")
    return {"ok": True, "rearmedCount": rearmed}


@app.post("/internal/pricing/revert")
def price_revert(req: PriceRevertRequest):
    try:
        with get_db() as s:
            result = revert_price_decision(s, req.shop_domain, req.variant_id, req.decision_id)
            logger.info(
                "price_revert_succeeded",
                shop_domain=req.shop_domain, variant_id=req.variant_id, decision_id=req.decision_id,
            )
            return {"ok": True, **result}
    except RevertError as exc:
        logger.warning(
            "price_revert_rejected",
            shop_domain=req.shop_domain, variant_id=req.variant_id, decision_id=req.decision_id, error=str(exc),
        )
        return {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception(
            "price_revert_failed",
            shop_domain=req.shop_domain, variant_id=req.variant_id, decision_id=req.decision_id,
        )
        return {"ok": False, "error": "Something went wrong reverting this price change."}


@app.post("/internal/matches/review")
def matches_review(req: MatchReviewRequest):
    if req.action not in ("confirm", "reject"):
        return {"ok": False, "error": f"Unknown action: {req.action!r}. Must be 'confirm' or 'reject'."}
    try:
        with get_db() as s:
            fn = confirm_match if req.action == "confirm" else reject_match
            result = fn(s, req.shop_domain, req.match_id)
            logger.info(
                "match_review_succeeded",
                shop_domain=req.shop_domain, match_id=req.match_id, action=req.action,
            )
            return {"ok": True, **result}
    except MatchReviewError as exc:
        logger.warning(
            "match_review_rejected",
            shop_domain=req.shop_domain, match_id=req.match_id, action=req.action, error=str(exc),
        )
        return {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception(
            "match_review_failed",
            shop_domain=req.shop_domain, match_id=req.match_id, action=req.action,
        )
        return {"ok": False, "error": "Something went wrong reviewing this match."}


@app.get("/health")
def health():
    return {"status": "ok"}
