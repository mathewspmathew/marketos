"""
services/chatbot_svc/app.py

FastAPI application for the chatbot service.

Endpoints:
  POST /chat          — SSE stream: creates/reuses a ChatSession, persists
                        the user message, runs the Pydantic-AI agent, emits
                        SSE events (open, text, ask, preview, done, error),
                        and persists assistant/tool messages.
  POST /apply-callback — receives apply results from the React Router side
                        (currently RR routes do not post a callback, but the
                        endpoint is reserved for symmetry). Inserts a tool
                        message.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import structlog
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from services.chatbot_svc.schemas import PaneConfigInput, QueryCandidate
from services.chatbot_svc.tools import query_studio as t_query_studio
from services.chatbot_svc.tools.toggle_settings import compute_disable_counts
from services.chatbot_svc.agent import agent
from services.chatbot_svc.context import build_context
from services.chatbot_svc.deps import build_deps
from services.chatbot_svc.titling import maybe_set_title
from services.chatbot_svc import sessions as sessions_svc
from services.chatbot_svc.tools.ask import AskUserRequested
from services.common import pane_config
from services.common.db import get_db
from services.common.logging_config import setup_logging
from services.common.models import ChatMessage, ChatPreview, ChatSession, ShopifyProduct

setup_logging()

logger = structlog.get_logger(__name__)

app = FastAPI(title="MarketOS Chatbot Service")

# Strong refs to fire-and-forget titling tasks so the event loop keeps them
# alive until completion (asyncio only holds weak refs to tasks).
_title_tasks: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# Pydantic request / response schemas
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    shop_domain: str
    user_id: str | None = None
    session_id: str | None = None
    message: str


class QueryStudioRequest(BaseModel):
    shop_domain: str
    product_id: str
    focus: str = ""
    mode: Literal["propose", "refine"] = "propose"
    prior: Optional[list[QueryCandidate]] = None
    instruction: Optional[str] = None


class ApplyCallback(BaseModel):
    preview_id: str
    result: dict


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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_session(
    shop: str,
    user_id: str | None,
    session_id: str | None,
) -> str:
    """Return an existing session_id or create a new ChatSession row."""
    if session_id:
        return session_id

    sid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(
            ChatSession(
                id=sid,
                shopDomain=shop,
                userId=user_id,
                createdAt=now,
                updatedAt=now,
            )
        )
    return sid


def _preview_event_data(preview: ChatPreview) -> dict:
    """Build the SSE `preview` payload from a ChatPreview row.

    Must include `change` so the client card can tell enable from disable
    (DynamicPricingCard branches on change.cardState) and `variantIds` for the
    affected count / frozen ids.
    """
    return {
        "preview_id": preview.id,
        "kind": preview.kind,
        "change": preview.change,
        "variantIds": preview.variantIds,
        "summary": preview.summary,
        "expires_at": preview.expiresAt.isoformat(),
    }


def _record(session_id: str, role: str, content: dict) -> None:
    """Persist a ChatMessage row with computed tokenCount."""
    from services.chatbot_svc.context import count_tokens

    with get_db() as s:
        s.add(
            ChatMessage(
                id=uuid.uuid4().hex,
                sessionId=session_id,
                role=role,
                content=content,
                tokenCount=count_tokens(content),
                createdAt=datetime.now(timezone.utc),
            )
        )


def _bind_request_context(shop_domain: str, session_id: str) -> None:
    """Bind shop_domain/session_id as structlog contextvars for the rest of
    this request. Every log line emitted anywhere during this request —
    including from deep inside apply_config.py's mutation functions — picks
    these up automatically, mirroring logging_config.py's Celery task_id
    binding."""
    structlog.contextvars.bind_contextvars(
        shop_domain=shop_domain,
        session_id=session_id,
    )


def _clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()


def _resolve_owned_product(session, shop_domain: str, product_id: str) -> ShopifyProduct:
    """Look up a ShopifyProduct and verify shop ownership. Raises ValueError
    if not found or not owned by shop_domain — endpoints convert this to a
    plain {ok: false, error} response."""
    product = session.get(ShopifyProduct, product_id)
    if product is None or product.shopDomain != shop_domain:
        raise ValueError(f"Product {product_id} not found in this shop.")
    return product


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/chat")
async def chat(req: ChatRequest):
    """Stream agent output as Server-Sent Events."""
    sid = _ensure_session(req.shop_domain, req.user_id, req.session_id)
    _bind_request_context(req.shop_domain, sid)
    # Build history BEFORE recording the new user message so the current
    # prompt isn't double-counted (pydantic-ai also receives it as the prompt).
    history = build_context(sid)
    _record(sid, "user", {"text": req.message})
    deps = build_deps(req.shop_domain, req.user_id, sid)

    async def event_stream():
        # Signal that the session is established and streaming begins.
        yield {"event": "open", "data": json.dumps({"session_id": sid})}

        try:
            try:
                result = await agent.run(req.message, deps=deps, message_history=history)
            except AskUserRequested as ask:
                # The agent raised a clarification request via the ask tool.
                payload = {"question": ask.question, "options": ask.options}
                _record(sid, "assistant", {"ask": payload})
                yield {"event": "ask", "data": json.dumps(payload)}
                yield {"event": "done", "data": "{}"}
                return

            # Pydantic-AI 1.x: AgentRunResult exposes `.output` (not `.data`).
            output = result.output if hasattr(result, "output") else getattr(result, "data", "")
            text = output if isinstance(output, str) else json.dumps(output)

            _record(sid, "assistant", {"text": text})
            # Fire-and-forget: generate a chat title from the first real
            # exchange. Skipped for refusals / already-titled sessions inside
            # maybe_set_title. Does not block the SSE response.
            # The maybe_set_title(...) call (recording call_args) runs synchronously here;
            # the coroutine body executes later via create_task.
            task = asyncio.create_task(maybe_set_title(sid, req.message, text))
            _title_tasks.add(task)
            task.add_done_callback(_title_tasks.discard)
            yield {"event": "text", "data": json.dumps({"text": text})}

            # Emit a preview event if the agent created a pending ChatPreview.
            with get_db() as s:
                last_preview = (
                    s.query(ChatPreview)
                    .filter(
                        ChatPreview.sessionId == sid,
                        ChatPreview.appliedAt.is_(None),
                    )
                    .order_by(ChatPreview.createdAt.desc())
                    .first()
                )
                if last_preview:
                    yield {
                        "event": "preview",
                        "data": json.dumps(_preview_event_data(last_preview)),
                    }

            yield {"event": "done", "data": "{}"}

        except Exception as exc:
            logger.exception("chat_request_failed", user_message=req.message)
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}

        finally:
            # Always close the httpx client regardless of outcome.
            await deps.http.aclose()
            _clear_request_context()

    return EventSourceResponse(event_stream())


@app.post("/query-studio")
async def query_studio(req: QueryStudioRequest):
    """Stateless Query Studio turn: propose 3 candidate competitor-search queries,
    or refine the prior ones per an instruction. Shop-scoped via the engine."""
    try:
        if req.mode == "refine":
            cands = t_query_studio.refine_queries(
                req.shop_domain, req.product_id, req.focus,
                req.prior or [], req.instruction or "",
            )
        else:
            cands = t_query_studio.propose_queries(req.shop_domain, req.product_id, req.focus)
        return {"candidates": [c.model_dump() for c in cands]}
    except Exception as exc:
        logger.exception(
            "query_studio_request_failed",
            shop_domain=req.shop_domain, product_id=req.product_id,
        )
        raise HTTPException(status_code=500, detail="Query Studio request failed.") from exc


@app.post("/apply-callback")
async def apply_callback(
    cb: ApplyCallback,
    x_internal_token: str = Header(default=""),
):
    """
    Receive the result of an apply action from the React Router side.

    Currently RR routes do not post a callback — this endpoint is reserved
    for future symmetry.  It records a tool message so the conversation
    history reflects what was applied.
    """
    if x_internal_token != os.environ.get("INTERNAL_API_TOKEN", ""):
        raise HTTPException(status_code=403, detail="Forbidden")

    with get_db() as s:
        preview = s.get(ChatPreview, cb.preview_id)
        if preview:
            _record(
                preview.sessionId,
                "tool",
                {
                    "tool_name": "apply",
                    "preview_id": cb.preview_id,
                    "tool_result": cb.result,
                },
            )

    return {"ok": True}


@app.post("/internal/dynamic-pricing/apply")
async def dynamic_pricing_apply(req: DynamicPricingApplyRequest):
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
async def dynamic_pricing_pause(req: DynamicPricingProductRequest):
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
async def dynamic_pricing_resume(req: DynamicPricingProductRequest):
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
async def dynamic_pricing_delete_preview(shop_domain: str, product_id: str):
    try:
        return {"ok": True, **compute_disable_counts(shop_domain, product_id)}
    except Exception:
        logger.exception(
            "dynamic_pricing_http_delete_preview_failed",
            shop_domain=shop_domain, product_id=product_id,
        )
        return {"ok": False, "error": "Something went wrong previewing the delete for this product."}


@app.post("/internal/dynamic-pricing/delete")
async def dynamic_pricing_delete(req: DynamicPricingDeleteRequest):
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


@app.get("/sessions")
async def list_sessions(shop_domain: str):
    """List a shop's chat sessions, newest first, with message counts."""
    return {"sessions": sessions_svc.list_sessions(shop_domain)}


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, shop_domain: str):
    """Return a chat's messages as front-end turns. 404 if not owned by shop."""
    turns = sessions_svc.get_turns(shop_domain, session_id)
    if turns is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"turns": turns}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, shop_domain: str):
    """Delete one chat owned by shop. 404 if not found / not owned."""
    if not sessions_svc.delete_session(shop_domain, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@app.delete("/sessions")
async def delete_all_sessions(shop_domain: str):
    """Delete all chats for a shop."""
    return {"ok": True, "deleted": sessions_svc.delete_all_sessions(shop_domain)}
