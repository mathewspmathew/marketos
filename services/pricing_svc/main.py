"""
services/pricing_svc/main.py

Deterministic rule engine. Reads VariantCompetitorStats + active PricingRule
+ ShopifyVariant + SalesAggregate, produces a PriceDecision row. No model
inference, no LLM, no network calls — pure function over DB rows.

Hot-path target: <10ms per decide. Costs are 4 small SELECTs + 1 INSERT.

Auto-apply gates (decision is recorded with blockedBy):
  - no CONFIRMED ProductLevelMatch        → "no_confirmed_match"
  - stats older than rule.maxStalenessSeconds → "stale_data"
  - active PromotionWindow covering this scope → "active_promotion"
  - PricingConfig.killSwitch on shop      → "kill_switch"

After hard gates pass, the candidate price is shaped by:
  - rule.ruleType (only MATCH_LOWEST in v1)
  - merchant-side nudge weighted by (1 - confidence): inventory days-of-stock
    and 7d-vs-28d velocity push price up/down when competitor signal is weak
  - guardrails (floor, ceiling, maxDailyDeltaPct × confidence)

The decision is always written. autoApply doesn't gate writes — only whether
shopify_writer_svc will pick it up (step 8). This lets the dashboard show
"would-have-done" suggestions even with autoApply off.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db
from services.pricing_svc.rules import (
    match_lowest,
    beat_by_pct,
    stay_above_cost_pct,
    match_tier_lowest,
)


# ─────────────────────────────────────────────────────────────────────────────
# Defaults (mirrors PricingConfig column defaults; used when no shop row exists)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_PRICING_CONFIG = {
    "sparseNudgeAmplitude": 0.02,
    "hotVelocityRatio":     1.3,
    "coolVelocityRatio":    0.7,
    "lowStockDays":         7,
    "highStockDays":        90,
    "killSwitch":           False,
}


# ─────────────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────────────

def _load_variant_context(session, shop_domain: str, variant_id: str) -> dict | None:
    """One round-trip: pull merchant variant, product, stats, sales, config,
    confirmed-match presence, kill switch, and active promo window."""
    row = session.execute(
        text("""
            SELECT sv.id, sv."currentPrice", sv.cost, sv."inventoryQuantity",
                   sv."autoPriceEnabled", sp.id AS product_id, sp.tags,
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
            "sparseNudgeAmplitude": float(row.sparseNudgeAmplitude) if row.sparseNudgeAmplitude is not None else DEFAULT_PRICING_CONFIG["sparseNudgeAmplitude"],
            "hotVelocityRatio":     float(row.hotVelocityRatio)     if row.hotVelocityRatio     is not None else DEFAULT_PRICING_CONFIG["hotVelocityRatio"],
            "coolVelocityRatio":    float(row.coolVelocityRatio)    if row.coolVelocityRatio    is not None else DEFAULT_PRICING_CONFIG["coolVelocityRatio"],
            "lowStockDays":         int(row.lowStockDays)           if row.lowStockDays         is not None else DEFAULT_PRICING_CONFIG["lowStockDays"],
            "highStockDays":        int(row.highStockDays)          if row.highStockDays        is not None else DEFAULT_PRICING_CONFIG["highStockDays"],
            "killSwitch":           bool(row.killSwitch) if row.killSwitch is not None else False,
        },
    }


def _resolve_rule(session, shop_domain: str, ctx: dict) -> dict | None:
    """Pick the most-specific active PricingRule for this variant.

    Priority: VARIANT > PRODUCT > SHOP. (COLLECTION/TAG scopes are defined in
    the schema for later; v1 doesn't resolve them — would need Shopify
    collection/tag wiring.)
    Within the same scope tier, the highest `priority` field wins.
    """
    row = session.execute(
        text("""
            SELECT id, "ruleType", params, "floorPrice", "ceilingPrice",
                   "maxDailyDeltaPct", "tierFilter", "maxStalenessSeconds",
                   "autoApply", priority, scope
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
        "priority":            row.priority,
        "scope":               row.scope,
    }


def _active_promotion(session, shop_domain: str, ctx: dict) -> bool:
    """True if any PromotionWindow covers this variant right now and is set
    to pause auto-pricing."""
    row = session.execute(
        text("""
            SELECT 1 FROM "PromotionWindow"
            WHERE "shopDomain" = :sd
              AND "pauseAutoPricing" = TRUE
              AND "startsAt" <= NOW()
              AND "endsAt"   >= NOW()
              AND (
                scope = 'SHOP'
                OR (scope = 'PRODUCT' AND "scopeRef" = :p)
                OR (scope = 'VARIANT' AND "scopeRef" = :v)
              )
            LIMIT 1
        """),
        {"sd": shop_domain, "p": ctx["productId"], "v": ctx["variantId"]},
    ).first()
    return row is not None


# ─────────────────────────────────────────────────────────────────────────────
# Decision math
# ─────────────────────────────────────────────────────────────────────────────

def _confidence_factor(ctx: dict, now: datetime) -> float:
    """Sigmoid over competitorCount × avg match confidence × freshness.

    Sigmoid keeps low-N from collapsing to zero — 1 competitor gives ~0.27,
    3 gives ~0.62, 5 gives ~0.82. That maps cleanly onto "small/medium/full"
    permitted price moves once multiplied with the rule's maxDailyDeltaPct.
    """
    stats = ctx["stats"]
    n = stats.get("competitorCount") or 0
    sigmoid_n = 1.0 / (1.0 + math.exp(-(n - 2)))  # midpoint at n=2
    amc = stats.get("avgMatchConfidence") or 0.0

    freshness = 1.0
    last = stats.get("lastUpdatedAt")
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (now - last).total_seconds() / 3600.0)
        freshness = max(0.1, 0.5 ** (age_hours / (7 * 24)))  # week half-life

    return max(0.0, min(1.0, sigmoid_n * amc * freshness))


def _merchant_nudge(candidate: float, ctx: dict, confidence: float) -> tuple[float, list[str]]:
    """Push price up/down based on inventory + sales velocity, weighted
    inversely to competitor confidence. Notes are returned for the snapshot.

    Returns (new_candidate, list_of_applied_nudge_descriptions).
    """
    cfg = ctx["config"]
    sales = ctx["sales"]
    notes: list[str] = []

    weight = 1.0 - confidence
    if weight <= 0:
        return candidate, notes
    amp = cfg["sparseNudgeAmplitude"]

    dos = sales.get("daysOfStock")
    if dos is not None:
        if dos < cfg["lowStockDays"]:
            factor = 1.0 + amp * weight
            candidate *= factor
            notes.append(f"low_stock(+{(factor-1)*100:.2f}%)")
        elif dos > cfg["highStockDays"]:
            factor = 1.0 - amp * weight
            candidate *= factor
            notes.append(f"high_stock({(factor-1)*100:.2f}%)")

    v7 = sales.get("orders7d", 0) / 7.0
    v28 = sales.get("orders28d", 0) / 28.0
    if v28 > 0:
        ratio = v7 / v28
        if ratio > cfg["hotVelocityRatio"]:
            factor = 1.0 + amp * weight
            candidate *= factor
            notes.append(f"hot_velocity(+{(factor-1)*100:.2f}%)")
        elif ratio < cfg["coolVelocityRatio"]:
            factor = 1.0 - amp * weight
            candidate *= factor
            notes.append(f"cool_velocity({(factor-1)*100:.2f}%)")

    return candidate, notes


def _clamp(candidate: float, rule: dict, ctx: dict, confidence: float) -> tuple[float, list[str]]:
    """Apply floor, ceiling, and confidence-scaled daily delta. Returns the
    clamped price and which clamps fired."""
    notes: list[str] = []
    current = ctx["currentPrice"]

    if rule["floorPrice"] is not None and candidate < rule["floorPrice"]:
        notes.append(f"floor_clamp({rule['floorPrice']})")
        candidate = rule["floorPrice"]
    if rule["ceilingPrice"] is not None and candidate > rule["ceilingPrice"]:
        notes.append(f"ceiling_clamp({rule['ceilingPrice']})")
        candidate = rule["ceilingPrice"]

    if rule["maxDailyDeltaPct"] is not None and current is not None and current > 0:
        effective = rule["maxDailyDeltaPct"] * confidence
        max_delta = current * (effective / 100.0)
        lo = current - max_delta
        hi = current + max_delta
        if candidate < lo:
            notes.append(f"delta_clamp_down(eff={effective:.2f}%)")
            candidate = lo
        elif candidate > hi:
            notes.append(f"delta_clamp_up(eff={effective:.2f}%)")
            candidate = hi

    return candidate, notes


def _round_to_charm(price: float, params: dict) -> float:
    """Charm pricing: by default round to 2 decimal places. If
    params['charm']='ninety_nine', land on .99 below the nearest whole."""
    if params.get("charm") == "ninety_nine":
        return max(0.0, int(price) - 0.01) if price >= 1.0 else round(price, 2)
    return round(price, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Rule dispatch
# ─────────────────────────────────────────────────────────────────────────────

_RULE_FUNCS = {
    "MATCH_LOWEST":         match_lowest.decide,
    "BEAT_BY_PCT":          beat_by_pct.decide,
    "STAY_ABOVE_COST_PCT":  stay_above_cost_pct.decide,
    "MATCH_TIER_LOWEST":    match_tier_lowest.decide,
}


def _apply_rule(rule: dict, ctx: dict, session, shop_domain: str) -> tuple[float | None, str]:
    fn = _RULE_FUNCS.get(rule["ruleType"])
    if fn is None:
        return None, f"unknown_rule_type:{rule['ruleType']}"
    # MATCH_TIER_LOWEST needs the rule's tierFilter; expose it on ctx so
    # the rule signature stays uniform.
    ctx_with_rule = {**ctx, "ruleTierFilter": rule.get("tierFilter") or []}
    return fn(ctx_with_rule, rule["params"], session, shop_domain)


# ─────────────────────────────────────────────────────────────────────────────
# Decision writer
# ─────────────────────────────────────────────────────────────────────────────

def _write_decision(
    session, shop_domain: str, ctx: dict, rule: dict | None,
    new_price: float | None, reason: str, blocked_by: str | None,
    confidence: float, stats_snapshot: dict, signals_snapshot: dict,
    rule_suggested_price: float | None = None,
    ml_suggested_price: float | None = None,
    ml_confidence: float | None = None,
    model_version: str | None = None,
) -> str:
    """Insert a PriceDecision row. Returns its id.

    `ruleSuggestedPrice` is the rule's pure output before merchant nudge +
    guardrails — useful for the UI to show "rule wanted X, we shipped Y".
    `mlSuggestedPrice` / `mlConfidence` / `modelVersion` are populated by
    elasticity_svc when Option A ML lands; null in v1.
    """
    import uuid
    pid = str(uuid.uuid4())
    old = ctx["currentPrice"] or 0.0
    new = new_price if new_price is not None else old
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
            "op": round(old, 2), "np": round(new, 2),
            "rsp": round(rule_suggested_price, 2) if rule_suggested_price is not None else None,
            "msp": round(ml_suggested_price, 2)   if ml_suggested_price   is not None else None,
            "mc":  round(ml_confidence, 3)        if ml_confidence        is not None else None,
            "mv":  model_version,
            "rs": reason, "bb": blocked_by, "cf": round(confidence, 3),
            "ss": json.dumps(stats_snapshot, default=_json_default),
            "sg": json.dumps(signals_snapshot, default=_json_default),
        },
    )
    return pid


def _json_default(o):
    if isinstance(o, (Decimal,)):
        return float(o)
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def decide_price(shop_domain: str, variant_id: str) -> dict:
    """Returns a small summary of what happened (also written as
    PriceDecision). Caller (Celery task) handles retries on exceptions.
    """
    now = datetime.now(timezone.utc)
    with get_db() as session:
        ctx = _load_variant_context(session, shop_domain, variant_id)
        if not ctx:
            return {"ok": False, "reason": "variant_not_found"}

        rule = _resolve_rule(session, shop_domain, ctx)

        signals = {
            "currentPrice":      ctx["currentPrice"],
            "inventoryQuantity": ctx["inventoryQuantity"],
            "daysOfStock":       ctx["sales"]["daysOfStock"],
            "orders7d":          ctx["sales"]["orders7d"],
            "orders28d":         ctx["sales"]["orders28d"],
            "hasConfirmedMatch": ctx["hasConfirmedMatch"],
            "autoPriceEnabled":  ctx["autoPriceEnabled"],
        }

        if rule is None:
            _write_decision(session, shop_domain, ctx, None,
                            None, "no_rule_configured", "no_rule",
                            0.0, ctx["stats"], signals)
            return {"ok": False, "reason": "no_rule_configured"}

        # ── Hard gates ─────────────────────────────────────────────────────
        if ctx["config"]["killSwitch"]:
            _write_decision(session, shop_domain, ctx, rule,
                            None, "kill_switch_on", "kill_switch",
                            0.0, ctx["stats"], signals)
            return {"ok": False, "reason": "kill_switch"}

        if rule["autoApply"] and not ctx["hasConfirmedMatch"]:
            _write_decision(session, shop_domain, ctx, rule,
                            None, "needs CONFIRMED match before auto-apply",
                            "no_confirmed_match", 0.0, ctx["stats"], signals)
            return {"ok": False, "reason": "no_confirmed_match"}

        stats_age_ok = True
        if ctx["stats"]["lastUpdatedAt"] is None:
            stats_age_ok = False
        else:
            last = ctx["stats"]["lastUpdatedAt"]
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() > rule["maxStalenessSeconds"]:
                stats_age_ok = False
        if not stats_age_ok:
            _write_decision(session, shop_domain, ctx, rule,
                            None, "stats stale or missing", "stale_data",
                            0.0, ctx["stats"], signals)
            return {"ok": False, "reason": "stale_data"}

        if _active_promotion(session, shop_domain, ctx):
            _write_decision(session, shop_domain, ctx, rule,
                            None, "promotion window active", "active_promotion",
                            0.0, ctx["stats"], signals)
            return {"ok": False, "reason": "active_promotion"}

        # ── Confidence & rule candidate ────────────────────────────────────
        confidence = _confidence_factor(ctx, now)

        candidate, rule_reason = _apply_rule(rule, ctx, session, shop_domain)
        rule_suggested = candidate  # pre-nudge, pre-clamp — for UI side-by-side
        if candidate is None:
            _write_decision(session, shop_domain, ctx, rule,
                            None, rule_reason, "no_candidate",
                            confidence, ctx["stats"], signals)
            return {"ok": False, "reason": "no_candidate"}

        # ── Merchant-side nudge ────────────────────────────────────────────
        candidate, nudge_notes = _merchant_nudge(candidate, ctx, confidence)

        # ── Guardrails ────────────────────────────────────────────────────
        # Implicit cost floor: never price below cost. Explicit rule.floorPrice
        # is also respected and may sit above cost.
        if ctx["cost"] is not None and candidate < ctx["cost"]:
            candidate = ctx["cost"]
            nudge_notes.append(f"cost_floor({ctx['cost']})")

        candidate, clamp_notes = _clamp(candidate, rule, ctx, confidence)

        candidate = _round_to_charm(candidate, rule["params"])

        # ── No-op short-circuit ───────────────────────────────────────────
        if ctx["currentPrice"] is not None and abs(candidate - ctx["currentPrice"]) < 0.005:
            _write_decision(session, shop_domain, ctx, rule,
                            candidate, "no_change", "noop",
                            confidence, ctx["stats"],
                            {**signals, "candidateBeforeRound": candidate,
                             "nudges": nudge_notes, "clamps": clamp_notes},
                            rule_suggested_price=rule_suggested)
            return {"ok": True, "noop": True, "price": candidate}

        # ── Final decision ────────────────────────────────────────────────
        reason_parts = [rule_reason] + nudge_notes + clamp_notes
        decision_id = _write_decision(
            session, shop_domain, ctx, rule,
            candidate, " | ".join(reason_parts), None,
            confidence, ctx["stats"],
            {**signals, "nudges": nudge_notes, "clamps": clamp_notes,
             "confidence": confidence},
            rule_suggested_price=rule_suggested,
        )

    # Hand off to the writer when the rule is set to auto-apply. The
    # writer re-checks every gate (CONFIRMED match, kill switch, no-op,
    # etc.) before touching Shopify, so this enqueue is just a hint.
    if rule["autoApply"]:
        app.send_task(
            "shopify_writer.apply_decision",
            args=[decision_id],
            queue="writer_queue",
        )

    return {"ok": True, "decisionId": decision_id,
            "oldPrice": ctx["currentPrice"], "newPrice": candidate,
            "confidence": confidence}


# ─────────────────────────────────────────────────────────────────────────────
# Celery task
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name="pricing.decide_for_variant", bind=True, max_retries=3, default_retry_delay=15)
def decide_for_variant(self, shop_domain: str, shopify_variant_id: str):
    try:
        return decide_price(shop_domain, shopify_variant_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            print(f"[pricing] variant {shopify_variant_id} permanently failed: {exc}", flush=True)
            return {"ok": False, "reason": "exception", "error": str(exc)}
        raise self.retry(exc=exc)
