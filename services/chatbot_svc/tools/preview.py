from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone
from services.common.db import get_db
from services.common.models import ChatPreview
from services.chatbot_svc.schemas import (
    ScopeFilter, PriceChange, PreviewSummary,
)
from services.chatbot_svc.tools.search import structured_search
from services.chatbot_svc.tools.toggle_settings import (
    resolve_enable_settings, compute_disable_counts,
)

PREVIEW_TTL = timedelta(minutes=5)


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
    if rows:
        human = (
            f"I'll change prices on {len(rows)} variant(s) "
            f"(min ${summary_dict['minNew']}, max ${summary_dict['maxNew']}, "
            f"avg ${summary_dict['avgNew']:.2f}). Apply?"
        )
    else:
        human = "No variants match that scope. Want to broaden it?"
    return PreviewSummary(
        preview_id=preview_id, kind="price_change", count=len(rows),
        sample_rows=sample, min_new=summary_dict["minNew"],
        max_new=summary_dict["maxNew"], avg_new=summary_dict["avgNew"],
        revenue_delta_est=summary_dict["revenueDeltaEst"],
        human_summary=human, expires_at=(now + PREVIEW_TTL).isoformat(),
    )


def preview_dynamic_pricing_toggle(shop_domain: str, session_id: str,
                                   scope: ScopeFilter, enabled: bool) -> PreviewSummary:
    rows = structured_search(shop_domain, scope, limit=1000)
    product_ids = sorted({r.product_id for r in rows})
    sample = rows[:10]
    now = datetime.now(timezone.utc)
    preview_id = uuid.uuid4().hex

    # Feature A is single-product by design: the system prompt instructs the
    # agent to toggle one product at a time, so product_ids is normally length 1.
    # The enable settings / disable counts below are therefore resolved for the
    # first (representative) product rather than aggregated across the scope.

    summary_dict = {
        "count": len(product_ids),
        "sampleRows": [r.model_dump() for r in sample],
        "minNew": None, "maxNew": None, "avgNew": None, "revenueDeltaEst": None,
    }

    if enabled:
        settings = resolve_enable_settings(shop_domain, product_ids)
        change = {
            "enabled": True,
            "rescrape": False,
            "numResults": settings["numResults"],
            "listingExpansionCap": settings["listingExpansionCap"],
            "query": settings["query"],
        }
        summary_dict["enable"] = settings
        human = (
            f"I'll enable dynamic pricing on {len(product_ids)} product(s). "
            f"You can rescrape now (off by default) — I'd search ~{settings['numResults']} "
            f"competitor sites and up to {settings['listingExpansionCap']} products per listing page. "
            f"Confirm below."
        )
    else:
        counts = (
            compute_disable_counts(shop_domain, product_ids[0])
            if product_ids else
            {"competitor_products": 0, "discovered_links": 0, "price_stats_variants": 0}
        )
        change = {"enabled": False}
        summary_dict["deleteCounts"] = counts
        human = (
            f"I'll turn off dynamic pricing on {len(product_ids)} product(s). "
            f"Choose Pause (keep data) or Delete "
            f"({counts['competitor_products']} competitor products, "
            f"{counts['discovered_links']} discovered links). Confirm below."
        )

    with get_db() as s:
        s.add(ChatPreview(
            id=preview_id, sessionId=session_id, shopDomain=shop_domain,
            kind="dynamic_pricing_toggle",
            scopeFilter=scope.model_dump(), change=change,
            variantIds=product_ids,
            summary=summary_dict, expiresAt=now + PREVIEW_TTL, createdAt=now,
        ))

    return PreviewSummary(
        preview_id=preview_id, kind="dynamic_pricing_toggle",
        count=len(product_ids), sample_rows=sample, human_summary=human,
        expires_at=(now + PREVIEW_TTL).isoformat(),
    )
