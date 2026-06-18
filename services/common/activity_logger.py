"""
services/common/activity_logger.py

Helper functions to log ActivityEvent records for pricing pipeline visibility.
All functions log synchronously (inline) with minimal DB overhead.
"""
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import insert

from services.common.db import get_db
from services.common.models import ActivityEvent


def log_competitor_observation(
    shop_domain: str,
    competitor_variant_id: str,
    competitor_product_id: str,
    price: Decimal,
    is_in_stock: bool,
    competitor_title: Optional[str] = None,
    competitor_domain: Optional[str] = None,
) -> None:
    """Log a competitor price observation event."""
    try:
        summary = f"Competitor price: ₹{float(price)}"
        if competitor_title:
            summary = f"{competitor_title[:40]}: ₹{float(price)}"
        if not is_in_stock:
            summary += " (out of stock)"

        with get_db() as session:
            session.execute(
                insert(ActivityEvent).values(
                    id=str(uuid.uuid4()),
                    shopDomain=shop_domain,
                    eventType="COMPETITOR_OBSERVATION",
                    occurredAt=datetime.now(timezone.utc),
                    summary=summary,
                    details=json.dumps({
                        "competitorTitle": competitor_title,
                        "competitorDomain": competitor_domain,
                        "price": float(price),
                        "isInStock": is_in_stock,
                    }),
                    competitorVariantId=competitor_variant_id,
                    competitorProductId=competitor_product_id,
                    competitorPrice=price,
                    competitorIsInStock=is_in_stock,
                )
            )
            session.commit()
    except Exception as e:
        print(f"[!] ActivityEvent log error (observation): {e}")


def log_decision_made(
    shop_domain: str,
    price_decision_id: str,
    shopify_product_id: str,
    shopify_variant_id: str,
    old_price: Decimal,
    new_price: Decimal,
    change_pct: Optional[float],
    ref_price: Optional[Decimal],
    competitors_used: int,
    tier_at_decision: Optional[str],
    top_matches_json: Optional[dict] = None,
    skip_reason: Optional[str] = None,
) -> None:
    """Log a pricing decision (made or skipped)."""
    try:
        if skip_reason:
            event_type = "DECISION_SKIPPED"
            summary = f"Decision skipped: {skip_reason}"
        else:
            event_type = "DECISION_MADE"
            pct_str = f"+{change_pct*100:.1f}%" if change_pct and change_pct > 0 else f"{change_pct*100:.1f}%"
            summary = f"Price decided: ₹{float(old_price)} → ₹{float(new_price)} ({pct_str})"

        with get_db() as session:
            session.execute(
                insert(ActivityEvent).values(
                    id=str(uuid.uuid4()),
                    shopDomain=shop_domain,
                    eventType=event_type,
                    occurredAt=datetime.now(timezone.utc),
                    summary=summary,
                    details=json.dumps({
                        "competitorsUsed": competitors_used,
                        "tier": tier_at_decision,
                        "topMatches": top_matches_json or [],
                    }),
                    priceDecisionId=price_decision_id,
                    shopifyProductId=shopify_product_id,
                    shopifyVariantId=shopify_variant_id,
                    oldPrice=old_price,
                    newPrice=new_price,
                    changePct=change_pct,
                    refPrice=ref_price,
                    competitorsUsed=competitors_used,
                    tierAtDecision=tier_at_decision,
                    topMatchesJson=top_matches_json,
                    skipReason=skip_reason,
                )
            )
            session.commit()
    except Exception as e:
        print(f"[!] ActivityEvent log error (decision): {e}")


def log_decision_applied(
    shop_domain: str,
    price_decision_id: str,
    shopify_product_id: str,
    shopify_variant_id: str,
) -> None:
    """Log a successful Shopify price apply event."""
    try:
        with get_db() as session:
            session.execute(
                insert(ActivityEvent).values(
                    id=str(uuid.uuid4()),
                    shopDomain=shop_domain,
                    eventType="DECISION_APPLIED",
                    occurredAt=datetime.now(timezone.utc),
                    summary="Price applied to Shopify",
                    details=json.dumps({}),
                    priceDecisionId=price_decision_id,
                    shopifyProductId=shopify_product_id,
                    shopifyVariantId=shopify_variant_id,
                )
            )
            session.commit()
    except Exception as e:
        print(f"[!] ActivityEvent log error (applied): {e}")


def log_decision_failed(
    shop_domain: str,
    price_decision_id: str,
    shopify_product_id: str,
    shopify_variant_id: str,
    error_message: str,
) -> None:
    """Log a failed Shopify price apply event."""
    try:
        with get_db() as session:
            session.execute(
                insert(ActivityEvent).values(
                    id=str(uuid.uuid4()),
                    shopDomain=shop_domain,
                    eventType="DECISION_FAILED",
                    occurredAt=datetime.now(timezone.utc),
                    summary=f"Price apply failed: {error_message[:60]}",
                    details=json.dumps({"errorMessage": error_message}),
                    priceDecisionId=price_decision_id,
                    shopifyProductId=shopify_product_id,
                    shopifyVariantId=shopify_variant_id,
                    applyError=error_message,
                )
            )
            session.commit()
    except Exception as e:
        print(f"[!] ActivityEvent log error (failed): {e}")
