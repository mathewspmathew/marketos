"""MATCH_LOWEST: candidate = weighted-min competitor price."""
from __future__ import annotations


def decide(ctx: dict, params: dict, session, shop_domain: str) -> tuple[float | None, str]:
    stats = ctx["stats"]
    weighted_min = stats.get("weightedMin")
    if weighted_min is not None:
        return float(weighted_min), f"match_lowest: weightedMin={weighted_min}"
    min_price = stats.get("minPrice")
    if min_price is not None:
        return float(min_price), f"match_lowest: minPrice={min_price} (unweighted)"
    return None, "match_lowest: no usable competitor price"
