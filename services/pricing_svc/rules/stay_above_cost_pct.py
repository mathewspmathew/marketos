"""STAY_ABOVE_COST_PCT: candidate is the lower of (competitor weighted-min)
and (cost × (1 + margin pct)) — i.e. match competitors but never sell at
less than the configured margin above cost.

Example: cost=600, params={"pct": 30}, weightedMin=900 →
  margin_floor = 600 × 1.30 = 780
  candidate    = max(competitor, margin_floor) = max(900, 780) = 900

Example: same cost+pct but weightedMin=700 →
  candidate    = max(700, 780) = 780  (margin protected)

If cost is unknown, the rule can't enforce its primary guardrail, so we
return None and the decision is recorded as no_candidate.
"""
from __future__ import annotations


def decide(ctx: dict, params: dict, session, shop_domain: str) -> tuple[float | None, str]:
    cost = ctx.get("cost")
    if cost is None:
        return None, "stay_above_cost_pct: variant cost is null"

    pct_raw = params.get("pct", 0)
    try:
        pct = float(pct_raw)
    except (TypeError, ValueError):
        return None, f"stay_above_cost_pct: invalid pct={pct_raw}"
    pct = max(0.0, min(500.0, pct))

    margin_floor = float(cost) * (1.0 + pct / 100.0)

    stats = ctx["stats"]
    competitor = stats.get("weightedMin") or stats.get("minPrice")
    if competitor is None:
        return margin_floor, (
            f"stay_above_cost_pct: no competitor price, using margin_floor="
            f"{margin_floor:.2f} (cost={cost}, pct={pct})"
        )

    candidate = max(float(competitor), margin_floor)
    return candidate, (
        f"stay_above_cost_pct: max(competitor={competitor}, "
        f"margin_floor={margin_floor:.2f}) = {candidate:.2f}"
    )
