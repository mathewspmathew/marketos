"""
services/elasticity_svc/inference.py

Use the trained quantile models to pick the price that maximizes expected
revenue, sweeping a bounded range around the current price. Returns the
suggestion + a confidence score derived from the P10–P90 spread.

Loading is cached per shop in-process. The training task writes a fresh
version.txt; we check that on every call and reload if it changed.
"""
from __future__ import annotations

import threading
from pathlib import Path

import joblib
import numpy as np

from services.elasticity_svc.features import (
    FEATURE_COLUMNS, context_to_features, VariantContext,
)


_MODELS_ROOT = Path(__file__).parent / "models"

# Sweep grid for finding the revenue-optimal price.
SWEEP_PCT = (0.70, 0.85, 0.92, 0.96, 1.00, 1.04, 1.08, 1.15, 1.30)

# In-memory model cache keyed by shop_domain; each entry holds the version
# string and the three regressors. Reload on version change.
_lock = threading.Lock()
_cache: dict[str, dict] = {}


def _safe_dir(shop_domain: str) -> Path:
    safe = shop_domain.replace("/", "_").replace(":", "_")
    return _MODELS_ROOT / safe


def _load_models(shop_domain: str) -> dict | None:
    """Return {'version','p10','p50','p90'} or None if no model exists."""
    d = _safe_dir(shop_domain)
    ver_file = d / "version.txt"
    if not ver_file.exists():
        return None
    version = ver_file.read_text().strip()

    with _lock:
        cached = _cache.get(shop_domain)
        if cached and cached["version"] == version:
            return cached
        try:
            p10 = joblib.load(d / "quantile_p10.joblib")
            p50 = joblib.load(d / "quantile_p50.joblib")
            p90 = joblib.load(d / "quantile_p90.joblib")
        except FileNotFoundError:
            return None
        entry = {"version": version, "p10": p10, "p50": p50, "p90": p90}
        _cache[shop_domain] = entry
        return entry


def suggest_price(shop_domain: str, ctx: VariantContext) -> dict | None:
    """Sweep prices ±30% around current and pick the one that maximizes the
    expected revenue (price × P50 demand). Returns:
      {price, confidence, modelVersion}
    or None when no model is available.

    Confidence = mean(P50) / (mean(P90 - P10) + 1).  Wider P10–P90 bands mean
    the model is uncertain → lower confidence. Normalized to [0,1].
    """
    models = _load_models(shop_domain)
    if not models:
        return None
    if ctx.price <= 0:
        return None

    candidates = []
    for pct in SWEEP_PCT:
        price = ctx.price * pct
        features = context_to_features(ctx, override_price=price)
        candidates.append((price, features))

    X = np.array([c[1] for c in candidates], dtype=np.float64)
    p10 = models["p10"].predict(X)
    p50 = models["p50"].predict(X)
    p90 = models["p90"].predict(X)

    # Clip predictions to non-negative — quantile loss can occasionally
    # spit out a small negative.
    p10 = np.clip(p10, 0, None)
    p50 = np.clip(p50, 0, None)
    p90 = np.clip(p90, 0, None)

    # Expected revenue at each price candidate.
    revenue = np.array([c[0] for c in candidates]) * p50
    best_i  = int(np.argmax(revenue))
    best_price = float(candidates[best_i][0])

    # Confidence: lower when the P10/P90 spread is large relative to the P50.
    # A perfectly certain model would have p90==p10==p50 → confidence near 1.0.
    spread = float(np.mean(p90 - p10))
    center = float(np.mean(p50)) + 1.0
    raw = center / (center + spread)        # in (0,1]
    confidence = max(0.0, min(1.0, raw))

    return {
        "price":        round(best_price, 2),
        "confidence":   round(confidence, 3),
        "modelVersion": models["version"],
    }
