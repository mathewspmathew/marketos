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

import structlog
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from services.chatbot_svc.agent import agent
from services.chatbot_svc.context import build_context
from services.chatbot_svc.deps import build_deps
from services.chatbot_svc.titling import maybe_set_title
from services.chatbot_svc import sessions as sessions_svc
from services.chatbot_svc.tools.ask import AskUserRequested
from services.common.db import get_db
from services.common.logging_config import setup_logging
from services.common.models import ChatMessage, ChatPreview, ChatSession

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


class ApplyCallback(BaseModel):
    preview_id: str
    result: dict


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


def _get_last_pending_preview_event(session_id: str) -> dict | None:
    """SSE `preview` event payload for the most recent not-yet-applied
    ChatPreview in a session, if any. Serializes to a dict *inside* the
    get_db() block — get_db()'s session.commit() expires ORM attributes,
    and the session closes right after, so returning the ORM object itself
    (instead of already-read plain values) would raise DetachedInstanceError
    on the first attribute access outside this function."""
    with get_db() as s:
        last_preview = (
            s.query(ChatPreview)
            .filter(
                ChatPreview.sessionId == session_id,
                ChatPreview.appliedAt.is_(None),
            )
            .order_by(ChatPreview.createdAt.desc())
            .first()
        )
        return _preview_event_data(last_preview) if last_preview else None


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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/chat")
async def chat(req: ChatRequest):
    """Stream agent output as Server-Sent Events. The blocking DB calls in
    here (_ensure_session, build_context, _record) are offloaded via
    run_in_threadpool so they don't block this single-process event loop
    while the real async work (agent.run, SSE streaming) is in flight —
    the calls are still awaited in order, so ordering is unchanged from
    the previous fully-synchronous version."""
    sid = await run_in_threadpool(_ensure_session, req.shop_domain, req.user_id, req.session_id)
    _bind_request_context(req.shop_domain, sid)
    # Build history BEFORE recording the new user message so the current
    # prompt isn't double-counted (pydantic-ai also receives it as the prompt).
    history = await run_in_threadpool(build_context, sid)
    await run_in_threadpool(_record, sid, "user", {"text": req.message})
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
                await run_in_threadpool(_record, sid, "assistant", {"ask": payload})
                yield {"event": "ask", "data": json.dumps(payload)}
                yield {"event": "done", "data": "{}"}
                return

            # Pydantic-AI 1.x: AgentRunResult exposes `.output` (not `.data`).
            output = result.output if hasattr(result, "output") else getattr(result, "data", "")
            text = output if isinstance(output, str) else json.dumps(output)

            await run_in_threadpool(_record, sid, "assistant", {"text": text})
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
            preview_event = await run_in_threadpool(_get_last_pending_preview_event, sid)
            if preview_event:
                yield {"event": "preview", "data": json.dumps(preview_event)}

            yield {"event": "done", "data": "{}"}

        except Exception as exc:
            logger.exception("chat_request_failed", user_message=req.message)
            yield {"event": "error", "data": json.dumps({"message": str(exc)})}

        finally:
            # Always close the httpx client regardless of outcome.
            await deps.http.aclose()
            _clear_request_context()

    return EventSourceResponse(event_stream())


@app.post("/apply-callback")
def apply_callback(
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


@app.get("/sessions")
def list_sessions(shop_domain: str):
    """List a shop's chat sessions, newest first, with message counts."""
    return {"sessions": sessions_svc.list_sessions(shop_domain)}


@app.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str, shop_domain: str):
    """Return a chat's messages as front-end turns. 404 if not owned by shop."""
    turns = sessions_svc.get_turns(shop_domain, session_id)
    if turns is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"turns": turns}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, shop_domain: str):
    """Delete one chat owned by shop. 404 if not found / not owned."""
    if not sessions_svc.delete_session(shop_domain, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@app.delete("/sessions")
def delete_all_sessions(shop_domain: str):
    """Delete all chats for a shop."""
    return {"ok": True, "deleted": sessions_svc.delete_all_sessions(shop_domain)}
