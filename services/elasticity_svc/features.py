"""
services/elasticity_svc/features.py

Build the feature matrix that the demand model trains on and predicts from.

Schema (each row = one variant snapshot):
  price                 — what the merchant was charging
  competitor_min        — weighted min across competitors at that time
  competitor_median     — weighted median
  competitor_count      — sample size
  avg_match_confidence  — average ProductMatch confidence
  inventory_quantity    — units on hand
  orders7d              — orders in trailing 7 days (also the TARGET if training)
  orders28d             — orders in trailing 28 days
  days_of_stock         — inventory / daily velocity

For inference we use the LIVE row (current stats + current variant state). For
training we pull historical applied PriceDecision rows (appliedAt IS NOT NULL)
joined back to the stats+sales snapshot at the time of that change. With sparse
history we bootstrap with synthetic samples (see training.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import text


FEATURE_COLUMNS = [
    "price",
    "competitor_min",
    "competitor_median",
    "competitor_count",
    "avg_match_confidence",
    "inventory_quantity",
    "orders28d",
    "days_of_stock",
]

# Index of the `price` column inside FEATURE_COLUMNS — needed by the inference
# code for the monotone_constraints array. Keep in sync with FEATURE_COLUMNS.
PRICE_FEATURE_INDEX = 0


@dataclass
class VariantContext:
    """Live snapshot used at inference time."""
    variantId: str
    price: float
    competitor_min: float | None
    competitor_median: float | None
    competitor_count: int
    avg_match_confidence: float
    inventory_quantity: int | None
    orders7d: int
    orders28d: int
    days_of_stock: float | None


def load_inference_context(session, shop_domain: str, variant_id: str) -> VariantContext | None:
    """Pull the current feature snapshot for one variant. Returns None if the
    variant doesn't exist or has no usable competitor stats."""
    row = session.execute(
        text("""
            SELECT sv.id,
                   sv."currentPrice"::float AS price,
                   vcs."weightedMin"::float        AS comp_min,
                   vcs."weightedMedian"::float     AS comp_med,
                   vcs."competitorCount"           AS comp_n,
                   vcs."avgMatchConfidence"::float AS amc,
                   sv."inventoryQuantity"          AS inv,
                   COALESCE(sa."orders7d", 0)     AS o7,
                   COALESCE(sa."orders28d", 0)    AS o28,
                   sa."daysOfStock"::float         AS dos
            FROM "ShopifyVariant" sv
            JOIN "ShopifyProduct" sp ON sp.id = sv."productId"
            LEFT JOIN "VariantCompetitorStats" vcs ON vcs."shopifyVariantId" = sv.id
            LEFT JOIN "SalesAggregate"        sa  ON sa."shopifyVariantId"  = sv.id
            WHERE sv.id = :v AND sp."shopDomain" = :sd
        """),
        {"v": variant_id, "sd": shop_domain},
    ).first()
    if not row:
        return None
    return VariantContext(
        variantId=row.id,
        price=float(row.price or 0),
        competitor_min=row.comp_min,
        competitor_median=row.comp_med,
        competitor_count=int(row.comp_n or 0),
        avg_match_confidence=float(row.amc or 0.5),
        inventory_quantity=row.inv,
        orders7d=int(row.o7),
        orders28d=int(row.o28),
        days_of_stock=row.dos,
    )


def context_to_features(ctx: VariantContext, override_price: float | None = None) -> list[float]:
    """Return a feature vector matching FEATURE_COLUMNS. `override_price`
    is used during the price sweep at inference time — we vary price while
    holding everything else constant."""
    price = override_price if override_price is not None else ctx.price
    return [
        float(price),
        float(ctx.competitor_min or price),
        float(ctx.competitor_median or price),
        float(ctx.competitor_count),
        float(ctx.avg_match_confidence),
        float(ctx.inventory_quantity or 0),
        float(ctx.orders28d),
        float(ctx.days_of_stock or 0),
    ]


def load_training_rows(session, shop_domain: str) -> list[tuple[list[float], float]]:
    """Pull every historical (features, orders7d) pair for a shop.

    Joins applied PriceDecisions (the events) with the variant's CURRENT
    stats/sales as a rough proxy for "what the world looked like" — a fuller
    backfill would snapshot stats at change time, but we don't store that.
    Good enough for v1 and the synthetic bootstrap will provide most of the
    training signal anyway.
    """
    rows = session.execute(
        text("""
            SELECT pd."newPrice"::float                   AS price,
                   vcs."weightedMin"::float               AS comp_min,
                   vcs."weightedMedian"::float            AS comp_med,
                   vcs."competitorCount"                  AS comp_n,
                   vcs."avgMatchConfidence"::float        AS amc,
                   sv."inventoryQuantity"                 AS inv,
                   COALESCE(sa."orders7d", 0)            AS o7,
                   COALESCE(sa."orders28d", 0)           AS o28,
                   sa."daysOfStock"::float                AS dos
            FROM "PriceDecision" pd
            JOIN "ShopifyVariant" sv ON sv.id = pd."shopifyVariantId"
            LEFT JOIN "VariantCompetitorStats" vcs ON vcs."shopifyVariantId" = sv.id
            LEFT JOIN "SalesAggregate"        sa  ON sa."shopifyVariantId"  = sv.id
            WHERE pd."shopDomain" = :sd
              AND pd."appliedAt" IS NOT NULL
              AND vcs."weightedMin" IS NOT NULL
        """),
        {"sd": shop_domain},
    ).all()
    out: list[tuple[list[float], float]] = []
    for r in rows:
        features = [
            float(r.price),
            float(r.comp_min),
            float(r.comp_med or r.price),
            float(r.comp_n or 0),
            float(r.amc or 0.5),
            float(r.inv or 0),
            float(r.o28),
            float(r.dos or 0),
        ]
        target = float(r.o7)
        out.append((features, target))
    return out
