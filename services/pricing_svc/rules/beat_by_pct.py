"""BEAT_BY_PCT: undercut competitor weighted-min by params.pct percent.

Example: params={"pct": 5} with weightedMin=1000 → candidate = 950.
The pct value is clamped to [0, 50] to prevent footgun configurations
that would race prices to zero.
"""
from __future__ import annotations


def decide(ctx: dict, params: dict, session, shop_domain: str) -> tuple[float | None, str]:
    stats = ctx["stats"]
    pct_raw = params.get("pct", 0)
    try:
        pct = float(pct_raw)
    except (TypeError, ValueError):
        return None, f"beat_by_pct: invalid pct={pct_raw}"
    pct = max(0.0, min(50.0, pct))

    base = stats.get("weightedMin") or stats.get("minPrice")
    if base is None:
        return None, "beat_by_pct: no usable competitor price"
    base = float(base)
    candidate = base * (1.0 - pct / 100.0)
    return candidate, f"beat_by_pct: {base} × (1 - {pct}%) = {candidate:.2f}"
