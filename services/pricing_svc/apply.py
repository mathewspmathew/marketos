"""
services/pricing_svc/apply.py

DB I/O for the pricing flow: loading variant context + active rule + promo
windows, calling the pure decision math in decide.py, writing the resulting
PriceDecision row, and handing off to the Shopify writer when the rule is
set to auto-apply.

Kept separate from decide.py so the math is testable without a database and
from main.py so Celery wiring isn't entangled with SQL.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.pricing_svc.decide import compute_decision, DecisionResult
from services.elasticity_svc.features import load_inference_context
from services.elasticity_svc.inference import suggest_price as ml_suggest_price


DEFAULT_PRICING_CONFIG = {
    "sparseNudgeAmplitude": 0.02,
    "hotVelocityRatio":     1.3,
    "coolVelocityRatio":    0.7,
    "lowStockDays":         7,
    "highStockDays":        90,
    "killSwitch":           False,
}


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def _load_variant_context(session, shop_domain: str, variant_id: str) -> dict | None:
    row = session.execute(
        text("""
            SELECT sv.id, sv."currentPrice", sv.cost, sv."inventoryQuantity",
                   sv."autoPriceEnabled", sv."useMlSuggestion",
                   sp.id AS product_id, sp.tags,
                   vcs."competitorCount", vcs."minPrice", vcs."weightedMin",
                   vcs."weightedMedian", vcs."volatility24h",
                   vcs."avgMatchConfidence", vcs."lastUpdatedAt",
                   sa."orders7d", sa."orders28d", sa."daysOfStock",
                   pc."sparseNudgeAmplitude", pc."hotVelocityRatio",
                   pc."coolVelocityRatio", pc."lowStockDays", pc."highStockDays",
                   pc."killSwitch",
                   EXISTS (
                     SELECT 1 FROM "ProductLevelMatch" plm
                     WHERE plm."shopifyProductId" = sp.id
                       AND ( plm."confidenceTier" = 'CONFIRMED'
                          OR plm."confirmedByMerchant" = TRUE )
                   ) AS has_confirmed
            FROM "ShopifyVariant" sv
            JOIN "ShopifyProduct" sp ON sp.id = sv."productId"
            LEFT JOIN "VariantCompetitorStats" vcs ON vcs."shopifyVariantId" = sv.id
            LEFT JOIN "SalesAggregate"        sa  ON sa."shopifyVariantId"  = sv.id
            LEFT JOIN "PricingConfig"         pc  ON pc."shopDomain"        = :sd
            WHERE sv.id = :v AND sp."shopDomain" = :sd
        """),
        {"v": variant_id, "sd": shop_domain},
    ).first()

    if not row:
        return None

    return {
        "variantId":           row.id,
        "currentPrice":        float(row.currentPrice) if row.currentPrice is not None else None,
        "cost":                float(row.cost) if row.cost is not None else None,
        "inventoryQuantity":   row.inventoryQuantity,
        "autoPriceEnabled":    bool(row.autoPriceEnabled),
        "useMlSuggestion":     bool(row.useMlSuggestion),
        "productId":           row.product_id,
        "productTags":         row.tags or [],
        "hasConfirmedMatch":   bool(row.has_confirmed),
        "stats": {
            "competitorCount":    row.competitorCount,
            "minPrice":           float(row.minPrice) if row.minPrice is not None else None,
            "weightedMin":        float(row.weightedMin) if row.weightedMin is not None else None,
            "weightedMedian":     float(row.weightedMedian) if row.weightedMedian is not None else None,
            "volatility24h":      float(row.volatility24h) if row.volatility24h is not None else None,
            "avgMatchConfidence": float(row.avgMatchConfidence) if row.avgMatchConfidence is not None else None,
            "lastUpdatedAt":      row.lastUpdatedAt,
        },
        "sales": {
            "orders7d":     row.orders7d or 0,
            "orders28d":    row.orders28d or 0,
            "daysOfStock":  float(row.daysOfStock) if row.daysOfStock is not None else None,
        },
        "config": {
            k: (type(DEFAULT_PRICING_CONFIG[k])(getattr(row, k)) if getattr(row, k) is not None
                else DEFAULT_PRICING_CONFIG[k])
            for k in DEFAULT_PRICING_CONFIG
        },
    }


def _resolve_rule(session, shop_domain: str, ctx: dict) -> dict | None:
    row = session.execute(
        text("""
            SELECT id, "ruleType", params, "floorPrice", "ceilingPrice",
                   "maxDailyDeltaPct", "tierFilter", "maxStalenessSeconds",
                   "autoApply", "mlBlendWeight", priority, scope
            FROM "PricingRule"
            WHERE "shopDomain" = :sd AND enabled = TRUE
              AND (
                (scope = 'VARIANT'    AND "scopeRef" = :v)
                OR (scope = 'PRODUCT' AND "scopeRef" = :p)
                OR (scope = 'SHOP'    AND "scopeRef" IS NULL)
              )
            ORDER BY CASE scope WHEN 'VARIANT' THEN 0 WHEN 'PRODUCT' THEN 1
                                WHEN 'SHOP' THEN 2 ELSE 3 END,
                     priority DESC
            LIMIT 1
        """),
        {"sd": shop_domain, "v": ctx["variantId"], "p": ctx["productId"]},
    ).first()
    if not row:
        return None
    return {
        "id":                  row.id,
        "ruleType":            row.ruleType,
        "params":              row.params or {},
        "floorPrice":          float(row.floorPrice)   if row.floorPrice   is not None else None,
        "ceilingPrice":        float(row.ceilingPrice) if row.ceilingPrice is not None else None,
        "maxDailyDeltaPct":    float(row.maxDailyDeltaPct) if row.maxDailyDeltaPct is not None else None,
        "tierFilter":          row.tierFilter or [],
        "maxStalenessSeconds": row.maxStalenessSeconds,
        "autoApply":           bool(row.autoApply),
        "mlBlendWeight":       float(row.mlBlendWeight) if row.mlBlendWeight is not None else 0.0,
        "priority":            row.priority,
        "scope":               row.scope,
    }


def _active_promotion(session, shop_domain: str, ctx: dict) -> bool:
    row = session.execute(
        text("""
            SELECT 1 FROM "PromotionWindow"
            WHERE "shopDomain" = :sd
              AND "pauseAutoPricing" = TRUE
              AND "startsAt" <= NOW() AND "endsAt"   >= NOW()
              AND ( scope = 'SHOP'
                 OR (scope = 'PRODUCT' AND "scopeRef" = :p)
                 OR (scope = 'VARIANT' AND "scopeRef" = :v))
            LIMIT 1
        """),
        {"sd": shop_domain, "p": ctx["productId"], "v": ctx["variantId"]},
    ).first()
    return row is not None


def _stats_fresh(ctx: dict, rule: dict, now: datetime) -> bool:
    last = ctx["stats"]["lastUpdatedAt"]
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() <= rule["maxStalenessSeconds"]


def _write_decision(
    session, shop_domain: str, ctx: dict, rule: dict | None,
    result: DecisionResult | None, reason: str, blocked_by: str | None,
    confidence: float,
) -> str:
    """Insert a PriceDecision row. `result` is None for early-exit blocks
    (no rule / kill switch / no confirmed match / stale / promo) where we
    short-circuit before running the math."""
    pid = str(uuid.uuid4())
    old = ctx["currentPrice"] or 0.0
    final = result.final_price if (result and result.final_price is not None) else old
    rule_p  = result.rule_price    if result else None
    ml_p    = result.ml_price      if result else None
    ml_c    = result.ml_confidence if result else None
    ml_v    = result.ml_version    if result else None
    signals = result.signals       if result else {}

    session.execute(
        text("""
            INSERT INTO "PriceDecision" (
                id, "shopDomain", "shopifyVariantId", "ruleId",
                "oldPrice", "newPrice", "ruleSuggestedPrice",
                "mlSuggestedPrice", "mlConfidence", "modelVersion",
                reason, "blockedBy", confidence,
                "statsSnapshot", "signalsSnapshot", "decidedAt"
            ) VALUES (
                :i, :sd, :v, :r,
                :op, :np, :rsp,
                :msp, :mc, :mv,
                :rs, :bb, :cf,
                CAST(:ss AS jsonb), CAST(:sg AS jsonb), NOW()
            )
        """),
        {
            "i":  pid, "sd": shop_domain, "v": ctx["variantId"],
            "r":  rule["id"] if rule else None,
            "op": round(old, 2), "np": round(final, 2),
            "rsp": round(rule_p, 2) if rule_p is not None else None,
            "msp": round(ml_p, 2)   if ml_p   is not None else None,
            "mc":  round(ml_c, 3)   if ml_c   is not None else None,
            "mv":  ml_v,
            "rs": reason, "bb": blocked_by, "cf": round(confidence, 3),
            "ss": json.dumps(ctx["stats"], default=_json_default),
            "sg": json.dumps(signals, default=_json_default),
        },
    )
    return pid


def _run_ml_inference(session, shop_domain: str, variant_id: str) -> dict | None:
    """Always invoke the model when one exists, so the row records what ML
    would have suggested. Whether to BLEND it into the final price is a
    separate decision made in decide.compute_decision."""
    try:
        ml_ctx = load_inference_context(session, shop_domain, variant_id)
        if ml_ctx is None:
            return None
        return ml_suggest_price(shop_domain, ml_ctx)
    except Exception as exc:
        print(f"[pricing] ml inference failed for {variant_id}: {exc}", flush=True)
        return None


# app.send_task(
#       "pricing.decide_for_variant",
#       args=["acme-shoes.myshopify.com", "shop-variant-42"],
#       queue="pricing_queue",
#so
# decide_for_variant(self,
#                      shop_domain="acme-shoes.myshopify.com",
#                      shopify_variant_id="shop-variant-42")
#       queue="pricing_queue",

def decide_price(shop_domain: str, variant_id: str) -> dict:
    """Public entry point. Loads inputs, runs decide.compute_decision,
    writes the row, dispatches the Shopify writer when auto-apply is on."""
    now = datetime.now(timezone.utc)
    with get_db() as session:
        ctx = _load_variant_context(session, shop_domain, variant_id)
        if not ctx:
            return {"ok": False, "reason": "variant_not_found"}

        rule = _resolve_rule(session, shop_domain, ctx)

        # ── Hard gates: block before running the math ─────────────────────
        if rule is None:
            _write_decision(session, shop_domain, ctx, None, None,
                            "no_rule_configured", "no_rule", 0.0)
            return {"ok": False, "reason": "no_rule_configured"}

        if ctx["config"]["killSwitch"]:
            _write_decision(session, shop_domain, ctx, rule, None,
                            "kill_switch_on", "kill_switch", 0.0)
            return {"ok": False, "reason": "kill_switch"}

        if rule["autoApply"] and not ctx["hasConfirmedMatch"]:
            _write_decision(session, shop_domain, ctx, rule, None,
                            "needs CONFIRMED match before auto-apply",
                            "no_confirmed_match", 0.0)
            return {"ok": False, "reason": "no_confirmed_match"}

        if not _stats_fresh(ctx, rule, now):
            _write_decision(session, shop_domain, ctx, rule, None,
                            "stats stale or missing", "stale_data", 0.0)
            return {"ok": False, "reason": "stale_data"}

        if _active_promotion(session, shop_domain, ctx):
            _write_decision(session, shop_domain, ctx, rule, None,
                            "promotion window active", "active_promotion", 0.0)
            return {"ok": False, "reason": "active_promotion"}

        # ── Run the math ──────────────────────────────────────────────────
        ml_pred = _run_ml_inference(session, shop_domain, ctx["variantId"])
        result = compute_decision(ctx, rule, ml_pred, session, shop_domain, now)

        decision_id = _write_decision(
            session, shop_domain, ctx, rule, result,
            result.reason, result.blocked_by, result.confidence,
        )

    # Dispatch to Shopify only for auto-apply rules that produced a real
    # change. The writer re-checks every gate before touching Shopify.
    if (rule["autoApply"]
            and result.final_price is not None
            and not result.is_noop
            and result.blocked_by is None):
        app.send_task(
            "shopify_writer.apply_decision",
            args=[decision_id],
            queue="writer_queue",
        )

    return {
        "ok": result.final_price is not None,
        "decisionId": decision_id,
        "oldPrice": ctx["currentPrice"],
        "newPrice": result.final_price,
        "rulePrice": result.rule_price,
        "mlPrice": result.ml_price,
        "confidence": result.confidence,
        "noop": result.is_noop,
    }
