# services/chatbot_svc/tools/panel.py
"""State-aware dynamic-pricing panel: ONE tool call freezes everything the
card needs into a ChatPreview row. The backend picks the card variant from
DB state — the LLM never chooses enable/disable or builds a scope.

Card states and allowed actions (mirrored by internal.apply-chat-flag.jsx):
  FRESH  -> ["enable"]            editable first-time setup form
  ACTIVE -> ["pause", "delete"]   read-only, product is running
  PAUSED -> ["resume", "delete"]  read-only, data kept from a previous run
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from services.common.db import get_db
from services.common.models import ChatPreview, DiscoveryJob, ShopifyProduct
from services.chatbot_svc.schemas import PanelSummary
from services.chatbot_svc.tools.enable_context import resolve_enable_context
from services.chatbot_svc.tools.toggle_settings import compute_disable_counts

PANEL_TTL = timedelta(minutes=30)

_STATE = {"FRESH": "FRESH", "ACTIVE": "ACTIVE", "PAUSED_WITH_DATA": "PAUSED"}
ALLOWED_ACTIONS = {
    "FRESH": ["enable"],
    "ACTIVE": ["pause", "delete"],
    "PAUSED": ["resume", "delete"],
}


def _human(card_state: str, title: str, ctx) -> str:
    if card_state == "FRESH":
        return (
            f"First-time setup for {title}. Review the search settings on the "
            f"card and press Start tracking."
        )
    if card_state == "ACTIVE":
        return (
            f"Dynamic pricing is already running on {title} "
            f"({ctx.competitors_found} competitor(s), {ctx.live_matches} matched). "
            f"Use the card to Pause or turn off and delete the data."
        )
    return (
        f"{title} was set up before — {ctx.competitors_found} competitor(s) are "
        f"kept. Use the card to Resume or turn off and delete the data."
    )


def open_dynamic_pricing_panel(shop_domain: str, session_id: str,
                               product_id: str) -> PanelSummary:
    ctx = resolve_enable_context(shop_domain, product_id)
    if ctx is None:
        raise RuntimeError(
            "That product id was not found in this shop. Call resolve_product "
            "first and use ONLY the product_id it returns."
        )
    card_state = _STATE[ctx.state]

    with get_db() as s:
        p = s.get(ShopifyProduct, product_id)
        latest_job = (
            s.query(DiscoveryJob)
            .filter(DiscoveryJob.shopDomain == shop_domain,
                    DiscoveryJob.shopifyProductId == product_id)
            .order_by(DiscoveryJob.requestedAt.desc())
            .first()
        )
        product_info = {
            "id": p.id,
            "title": p.title,
            "vendor": p.vendor,
            "category": p.categoryTop,
            "imageUrl": p.imageUrl,
            "dynamicPricingEnabled": bool(p.dynamicPricingEnabled),
            "latestJobStatus": latest_job.status if latest_job else None,
        }
        title = p.title

    change: dict = {
        "panel": True,
        "cardState": card_state,
        "allowedActions": ALLOWED_ACTIONS[card_state],
        # kept for backward-compat with the enable apply branch
        "enabled": card_state != "ACTIVE",
    }
    if card_state == "FRESH":
        change.update({
            "numResults": ctx.num_results,
            "listingExpansionCap": ctx.listing_expansion_cap,
            "query": ctx.current_query,
            "rescrape": False,
        })

    summary = {
        "count": 1,
        "sampleRows": [],
        "cardState": card_state,
        "product": product_info,
        "enableContext": ctx.model_dump(),
        "deleteCounts": (
            compute_disable_counts(shop_domain, product_id)
            if card_state != "FRESH" else None
        ),
    }

    now = datetime.now(timezone.utc)
    preview_id = uuid.uuid4().hex
    with get_db() as s:
        s.add(ChatPreview(
            id=preview_id, sessionId=session_id, shopDomain=shop_domain,
            kind="dynamic_pricing_toggle",
            scopeFilter={"product_ids": [product_id]},
            change=change, variantIds=[product_id], summary=summary,
            expiresAt=now + PANEL_TTL, createdAt=now,
        ))

    return PanelSummary(
        preview_id=preview_id, card_state=card_state, product_id=product_id,
        product_title=title, human_summary=_human(card_state, title, ctx),
        expires_at=(now + PANEL_TTL).isoformat(),
    )
