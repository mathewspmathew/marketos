from __future__ import annotations
from services.chatbot_svc.deps import AgentDeps
from services.chatbot_svc.schemas import ApplyResult


async def _post_apply(deps: AgentDeps, path: str, preview_id: str) -> ApplyResult:
    url = f"{deps.rr_base_url.rstrip('/')}{path}"
    resp = await deps.http.post(
        url,
        headers={"x-internal-token": deps.internal_token, "content-type": "application/json"},
        json={"preview_id": preview_id, "applied_by": deps.user_id},
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"apply_failed: {data.get('reason', 'unknown')}")
    return ApplyResult(
        preview_id=data["preview_id"],
        succeeded=data.get("succeeded", []),
        failed=data.get("failed", []),
    )


async def apply_price_change(deps: AgentDeps, preview_id: str) -> ApplyResult:
    return await _post_apply(deps, "/internal/apply-chat-price", preview_id)
