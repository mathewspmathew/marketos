from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone
from services.common.db import get_db
from services.common.models import ChatPreview
from services.chatbot_svc.schemas import (
    ScopeFilter, PriceChange, PreviewSummary,
)
from services.chatbot_svc.tools.search import structured_search

PREVIEW_TTL = timedelta(minutes=30)  # generous: the Query Studio detour can exceed 5 min


def _compute_new_price(current: float, change: PriceChange) -> float:
    if change.type == "percent":
        return round(current * (1 + change.value / 100), 2)
    if change.type == "absolute":
        return round(current + change.value, 2)
    if change.type == "set":
        return round(change.value, 2)
    raise ValueError(f"unknown change type {change.type}")


def preview_price_change(shop_domain: str, session_id: str,
                         scope: ScopeFilter, change: PriceChange) -> PreviewSummary:
    rows = structured_search(shop_domain, scope, limit=1000)
    if not rows:
        raise RuntimeError(
            "No products in your store match that selection, so there's nothing to "
            "preview. Resolve the product with resolve_product first."
        )
    variant_ids = [r.variant_id for r in rows]
    new_prices = [_compute_new_price(r.current_price, change) for r in rows]
    sample = rows[:10]
    summary_dict = {
        "count": len(rows),
        "sampleRows": [r.model_dump() for r in sample],
        "minNew": min(new_prices) if new_prices else None,
        "maxNew": max(new_prices) if new_prices else None,
        "avgNew": (sum(new_prices) / len(new_prices)) if new_prices else None,
        "revenueDeltaEst": (
            sum(np - r.current_price for np, r in zip(new_prices, rows)) if rows else 0
        ),
    }
    preview_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatPreview(
            id=preview_id,
            sessionId=session_id,
            shopDomain=shop_domain,
            kind="price_change",
            scopeFilter=scope.model_dump(),
            change=change.model_dump(),
            variantIds=variant_ids,
            summary=summary_dict,
            expiresAt=now + PREVIEW_TTL,
            createdAt=now,
        ))
    # rows is guaranteed non-empty here: the empty-match guard above raises otherwise.
    human = (
        f"I'll change prices on {len(rows)} variant(s) "
        f"(min ${summary_dict['minNew']}, max ${summary_dict['maxNew']}, "
        f"avg ${summary_dict['avgNew']:.2f}). Apply?"
    )
    return PreviewSummary(
        preview_id=preview_id, kind="price_change", count=len(rows),
        sample_rows=sample, min_new=summary_dict["minNew"],
        max_new=summary_dict["maxNew"], avg_new=summary_dict["avgNew"],
        revenue_delta_est=summary_dict["revenueDeltaEst"],
        human_summary=human, expires_at=(now + PREVIEW_TTL).isoformat(),
    )
