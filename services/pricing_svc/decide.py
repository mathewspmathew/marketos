"""
services/pricing_svc/decide.py

Pure decision math. No DB, no I/O, no Celery. Given a fully-loaded variant
context + active rule + optional ML prediction, compute the final price
along with the rule's pre-nudge suggestion, ML's suggestion, confidence,
and the human-readable reason trail.

The orchestration that loads inputs and writes PriceDecision lives in
apply.py — this module exists so the math is testable in isolation and so
the loop is comprehensible at a glance.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from services.pricing_svc.rules import (
    match_lowest,
    beat_by_pct,
    stay_above_cost_pct,
    match_tier_lowest,
)


_RULE_FUNCS = {
    "MATCH_LOWEST":         match_lowest.decide,
    "BEAT_BY_PCT":          beat_by_pct.decide,
    "STAY_ABOVE_COST_PCT":  stay_above_cost_pct.decide,
    "MATCH_TIER_LOWEST":    match_tier_lowest.decide,
}


@dataclass
class DecisionResult:
    """What compute_decision returns. apply.py turns this into a PriceDecision row."""
    final_price:    float | None        # what we'd ship (None when blocked)
    rule_price:     float | None        # rule output, pre-nudge / pre-clamp
    ml_price:       float | None        # what the ML model would have priced at
    ml_confidence:  float | None
    ml_version:     str | None
    confidence:     float
    reason:         str
    blocked_by:     str | None
    is_noop:        bool = False
    signals:        dict = field(default_factory=dict)


def _confidence_factor(ctx: dict, now: datetime) -> float:
    stats = ctx["stats"]
    n = stats.get("competitorCount") or 0
    sigmoid_n = 1.0 / (1.0 + math.exp(-(n - 2)))
    amc = stats.get("avgMatchConfidence") or 0.0

    freshness = 1.0
    last = stats.get("lastUpdatedAt")
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (now - last).total_seconds() / 3600.0)
        freshness = max(0.1, 0.5 ** (age_hours / (7 * 24)))

    return max(0.0, min(1.0, sigmoid_n * amc * freshness))


def _merchant_nudge(candidate: float, ctx: dict, confidence: float) -> tuple[float, list[str]]:
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
    if params.get("charm") == "ninety_nine":
        return max(0.0, int(price) - 0.01) if price >= 1.0 else round(price, 2)
    return round(price, 2)


def _apply_rule(rule: dict, ctx: dict, session, shop_domain: str) -> tuple[float | None, str]:
    """Rule dispatch. Takes session/shop_domain because some rules (e.g.
    MATCH_TIER_LOWEST) need to re-query competitor data. This is the only
    impurity in decide.py and it's confined to this function."""
    fn = _RULE_FUNCS.get(rule["ruleType"])
    if fn is None:
        return None, f"unknown_rule_type:{rule['ruleType']}"
    ctx_with_rule = {**ctx, "ruleTierFilter": rule.get("tierFilter") or []}
    return fn(ctx_with_rule, rule["params"], session, shop_domain)


def compute_decision(
    ctx: dict,
    rule: dict,
    ml_pred: dict | None,
    session,
    shop_domain: str,
    now: datetime,
) -> DecisionResult:
    """The whole decision pipeline as one pure-ish function.

    `ml_pred` is the model's output (already invoked by caller) or None when
    no model is trained / inference failed. Whether ML's price is *blended*
    into the final price is decided here based on rule.mlBlendWeight +
    variant.useMlSuggestion — but the prediction itself is always recorded
    on the DecisionResult so the UI can show side-by-side comparison.
    """
    confidence = _confidence_factor(ctx, now)
    signals_base = {
        "currentPrice":      ctx["currentPrice"],
        "inventoryQuantity": ctx["inventoryQuantity"],
        "daysOfStock":       ctx["sales"]["daysOfStock"],
        "orders7d":          ctx["sales"]["orders7d"],
        "orders28d":         ctx["sales"]["orders28d"],
        "hasConfirmedMatch": ctx["hasConfirmedMatch"],
        "autoPriceEnabled":  ctx["autoPriceEnabled"],
    }

    ml_price = ml_pred["price"]        if ml_pred else None
    ml_conf  = ml_pred["confidence"]   if ml_pred else None
    ml_ver   = ml_pred["modelVersion"] if ml_pred else None

    candidate, rule_reason = _apply_rule(rule, ctx, session, shop_domain)
    rule_suggested = candidate
    if candidate is None:
        return DecisionResult(
            final_price=None, rule_price=None,
            ml_price=ml_price, ml_confidence=ml_conf, ml_version=ml_ver,
            confidence=confidence, reason=rule_reason, blocked_by="no_candidate",
            signals=signals_base,
        )

    ml_notes: list[str] = []
    if (ml_price is not None
            and ctx["useMlSuggestion"]
            and rule["mlBlendWeight"] > 0):
        w = max(0.0, min(1.0, rule["mlBlendWeight"]))
        candidate = candidate * (1.0 - w) + ml_price * w
        ml_notes.append(f"ml_blend(w={w:.2f}, ml={ml_price}, conf={ml_conf})")

    candidate, nudge_notes = _merchant_nudge(candidate, ctx, confidence)

    # Implicit cost floor: never price below cost, regardless of rule.floorPrice.
    if ctx["cost"] is not None and candidate < ctx["cost"]:
        candidate = ctx["cost"]
        nudge_notes.append(f"cost_floor({ctx['cost']})")

    candidate, clamp_notes = _clamp(candidate, rule, ctx, confidence)
    candidate = _round_to_charm(candidate, rule["params"])

    is_noop = (ctx["currentPrice"] is not None
               and abs(candidate - ctx["currentPrice"]) < 0.005)
    reason = "no_change" if is_noop else " | ".join(
        [rule_reason] + ml_notes + nudge_notes + clamp_notes
    )

    return DecisionResult(
        final_price=candidate,
        rule_price=rule_suggested,
        ml_price=ml_price, ml_confidence=ml_conf, ml_version=ml_ver,
        confidence=confidence,
        reason=reason,
        blocked_by="noop" if is_noop else None,
        is_noop=is_noop,
        signals={
            **signals_base,
            "nudges": nudge_notes, "clamps": clamp_notes,
            "ml_notes": ml_notes, "confidence": confidence,
        },
    )
