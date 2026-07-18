# services/chatbot_svc/tools/panel.py
"""State-aware dynamic-pricing panel: ONE tool call freezes everything the
card needs into a ChatPreview row. The backend picks the card variant from
DB state — the LLM never chooses enable/disable or builds a scope.

Card states and allowed actions (this tool is unregistered/dead — see the
commented-out open_dynamic_pricing_panel in agent.py; the JS route that used
to consume this preview kind, internal.apply-chat-flag.jsx, was retired):
  FRESH  -> ["enable"]            editable first-time setup form
  ACTIVE -> ["pause", "delete"]   read-only, product is running
  PAUSED -> ["resume", "delete"]  read-only, data kept from a previous run
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text

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


def _active_stats(shop_domain: str, product_id: str) -> dict | None:
    """Read-only pricing stats for an ACTIVE product's card, no schema changes.

    Queries the pricing-worker's existing tables (same pattern as
    price_explanation.py): most recent applied PriceDecision across the
    product's variants, plus aggregated VariantCompetitorStats.
    """
    with get_db() as s:
        decision = s.execute(
            sa_text("""
                SELECT pd."oldPrice", pd."newPrice", pd."appliedAt", v.title AS variant_title
                FROM "PriceDecision" pd
                JOIN "ShopifyVariant" v ON v.id = pd."shopifyVariantId"
                WHERE v."productId" = :pid AND pd."shopDomain" = :shop
                  AND pd."appliedAt" IS NOT NULL
                ORDER BY pd."appliedAt" DESC
                LIMIT 1
            """),
            {"pid": product_id, "shop": shop_domain},
        ).mappings().first()

        competitors = s.execute(
            sa_text("""
                SELECT SUM(vcs."competitorCount") AS count,
                       MIN(vcs."minPrice") AS min_price,
                       AVG(vcs."median") AS median,
                       MAX(vcs."maxPrice") AS max_price
                FROM "VariantCompetitorStats" vcs
                JOIN "ShopifyVariant" v ON v.id = vcs."shopifyVariantId"
                WHERE v."productId" = :pid AND vcs."shopDomain" = :shop
            """),
            {"pid": product_id, "shop": shop_domain},
        ).mappings().first()

    if decision is None and (competitors is None or competitors["count"] is None):
        return None

    return {
        "lastPriceChange": {
            "oldPrice": float(decision["oldPrice"]),
            "newPrice": float(decision["newPrice"]),
            "appliedAt": decision["appliedAt"].isoformat(),
            "variantTitle": decision["variant_title"],
        } if decision is not None else None,
        "competitors": {
            "count": int(competitors["count"]),
            "minPrice": float(competitors["min_price"]) if competitors["min_price"] is not None else None,
            "median": float(competitors["median"]) if competitors["median"] is not None else None,
            "maxPrice": float(competitors["max_price"]) if competitors["max_price"] is not None else None,
        } if competitors is not None and competitors["count"] is not None else None,
    }


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
        # read only by the card's legacy-preview fallback, never by the apply route
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
        "stats": _active_stats(shop_domain, product_id) if card_state == "ACTIVE" else None,
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
