"""
services/elasticity_svc/training.py

Train three LightGBM quantile regressors (P10/P50/P90) that predict orders7d
from a feature vector. Monotone constraint: demand DECREASES as price
increases (constraint = -1 on the price column).

Synthetic bootstrap: if a shop has fewer than MIN_REAL_ROWS historical rows,
we generate samples around each live variant by perturbing price and
applying a constant-elasticity demand curve. The model trained on synthetic
data is clearly versioned "synthetic-v1" so dashboards/logs can show that
it's not yet learned from real data.

Output:
  models/{shop_domain}/quantile_p10.joblib
  models/{shop_domain}/quantile_p50.joblib
  models/{shop_domain}/quantile_p90.joblib
  models/{shop_domain}/version.txt    ← e.g. "synthetic-v1-20260516-031245"
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import lightgbm as lgb

from services.elasticity_svc.features import (
    FEATURE_COLUMNS, PRICE_FEATURE_INDEX,
    load_training_rows, context_to_features, load_inference_context,
)


MIN_REAL_ROWS         = 200    # below this, bootstrap with synthetic data
SYNTHETIC_PER_VARIANT = 80     # synthetic samples per live variant
PRICE_SWEEP_MIN_PCT   = 0.50   # synthetic prices sweep 50%..150% of current
PRICE_SWEEP_MAX_PCT   = 1.50
SYNTHETIC_ELASTICITY  = -1.5   # log-log demand elasticity (constant)
SYNTHETIC_NOISE_SIGMA = 0.15   # log-normal noise on demand

# Quantiles to fit (P10, P50, P90)
QUANTILES = (0.1, 0.5, 0.9)

_MODELS_ROOT = Path(__file__).parent / "models"


def _model_dir(shop_domain: str) -> Path:
    safe = shop_domain.replace("/", "_").replace(":", "_")
    d = _MODELS_ROOT / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _generate_synthetic(session, shop_domain: str) -> list[tuple[list[float], float]]:
    """Generate (features, orders7d) pairs around each live variant by sweeping
    price under a constant-elasticity demand curve. Anchored to the variant's
    actual orders28d so the curve is in the right zone for that product."""
    from sqlalchemy import text
    rows = session.execute(
        text("""
            SELECT sv.id, sv."currentPrice"::float AS price,
                   vcs."weightedMin"::float AS comp_min,
                   vcs."weightedMedian"::float AS comp_med,
                   vcs."competitorCount"    AS comp_n,
                   vcs."avgMatchConfidence"::float AS amc,
                   sv."inventoryQuantity"   AS inv,
                   COALESCE(sa."orders7d", 0)  AS o7,
                   COALESCE(sa."orders28d", 0) AS o28,
                   sa."daysOfStock"::float  AS dos
            FROM "ShopifyVariant" sv
            JOIN "ShopifyProduct" sp ON sp.id = sv."productId"
            LEFT JOIN "VariantCompetitorStats" vcs ON vcs."shopifyVariantId" = sv.id
            LEFT JOIN "SalesAggregate" sa ON sa."shopifyVariantId" = sv.id
            WHERE sp."shopDomain" = :sd
              AND sv."currentPrice" > 0
        """),
        {"sd": shop_domain},
    ).all()

    out: list[tuple[list[float], float]] = []
    for r in rows:
        anchor_price  = float(r.price)
        anchor_orders = max(1.0, float(r.o7))  # avoid log(0)
        for _ in range(SYNTHETIC_PER_VARIANT):
            ratio = random.uniform(PRICE_SWEEP_MIN_PCT, PRICE_SWEEP_MAX_PCT)
            p = anchor_price * ratio
            # constant elasticity: demand ∝ price^elasticity (elasticity is
            # negative → demand decreases as price increases)
            demand_mean = anchor_orders * (ratio ** SYNTHETIC_ELASTICITY)
            noise = random.gauss(0, SYNTHETIC_NOISE_SIGMA)
            demand = max(0.0, demand_mean * (2.71828 ** noise))
            features = [
                p,
                float(r.comp_min or p),
                float(r.comp_med or p),
                float(r.comp_n or 0),
                float(r.amc or 0.5),
                float(r.inv or 0),
                float(r.o28),
                float(r.dos or 0),
            ]
            out.append((features, demand))
    return out


def _train_quantile(X: np.ndarray, y: np.ndarray, quantile: float) -> lgb.LGBMRegressor:
    """One LightGBM regressor at a given quantile, with monotone constraint
    on the price feature (demand decreases as price rises)."""
    monotone = [0] * len(FEATURE_COLUMNS)
    monotone[PRICE_FEATURE_INDEX] = -1  # price → demand is decreasing

    model = lgb.LGBMRegressor(
        objective="quantile",
        alpha=quantile,
        learning_rate=0.05,
        n_estimators=300,
        num_leaves=15,
        min_child_samples=20,
        monotone_constraints=monotone,
        verbose=-1,
    )
    model.fit(X, y)
    return model


def train_for_shop(session, shop_domain: str) -> dict:
    """Pull data, bootstrap with synthetic if sparse, train P10/P50/P90,
    persist to disk. Returns {version, n_real, n_synthetic, n_features}."""
    real_rows = load_training_rows(session, shop_domain)
    rows = list(real_rows)

    n_synthetic = 0
    if len(rows) < MIN_REAL_ROWS:
        synth = _generate_synthetic(session, shop_domain)
        rows.extend(synth)
        n_synthetic = len(synth)

    if not rows:
        return {"ok": False, "reason": "no_data", "n_real": 0, "n_synthetic": 0}

    X = np.array([r[0] for r in rows], dtype=np.float64)
    y = np.array([r[1] for r in rows], dtype=np.float64)

    out_dir = _model_dir(shop_domain)
    for q in QUANTILES:
        model = _train_quantile(X, y, q)
        joblib.dump(model, out_dir / f"quantile_p{int(q*100):02d}.joblib")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prefix = "synthetic" if len(real_rows) < MIN_REAL_ROWS else "live"
    version = f"{prefix}-v1-{stamp}"
    (out_dir / "version.txt").write_text(version)
    (out_dir / "n_real.txt").write_text(str(len(real_rows)))
    (out_dir / "n_synthetic.txt").write_text(str(n_synthetic))

    return {
        "ok": True,
        "version": version,
        "n_real": len(real_rows),
        "n_synthetic": n_synthetic,
        "n_features": len(FEATURE_COLUMNS),
    }
