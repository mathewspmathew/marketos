"""
services/pricing_svc/stats.py

Maintains VariantCompetitorStats: the per-merchant-variant summary the
pricing engine reads in its hot path. One row per ShopifyVariant.

Inputs
  - ProductMatch (which competitor variants are matched, with confidence)
  - Competitor    (weight + enabled flag per scraped domain)
  - CompetitorPriceObservation (latest price per competitor variant; the
    last 24h of observations across the matched set for volatility)

Filter:
  - Drop weak matches whose confidence < LIKELY threshold (handled by the
    pricing-side confirmed-match gate anyway; here we just refuse to count
    obviously irrelevant competitors in stats).
  - Drop disabled competitors.
  - Drop observations whose competitor variant is currently out of stock —
    they're not actually sellable.
  - Drop observations older than MAX_FRESHNESS_DAYS.

Weighting:
  observation_weight = Competitor.weight × ProductMatch.confidence × freshness_decay(age)
  freshness_decay = 0.5 ** (age_hours / FRESHNESS_HALFLIFE_HOURS), floored at 0.05

Outputs:
  weightedMin    = min price among observations whose weight > 0
  weightedMedian = weighted median across observations
  min/p25/median/p75/max = unweighted percentiles (kept for UI displays)
  volatility24h  = stddev/mean of prices in last 24h across the matched set
  avgMatchConfidence = mean ProductMatch.confidence
  competitorCount    = count of qualifying observations

After upsert, enqueues pricing_svc to decide a new price for this variant.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db


FRESHNESS_HALFLIFE_HOURS = 7 * 24       # one week
MAX_FRESHNESS_DAYS       = 60
FRESHNESS_FLOOR          = 0.05
MIN_USABLE_CONFIDENCE    = 0.65         # LIKELY threshold


# ─────────────────────────────────────────────────────────────────────────────
# Statistics helpers
# ─────────────────────────────────────────────────────────────────────────────

def _as_utc(dt: datetime) -> datetime:
    """Normalize naive datetimes (some Postgres adapters drop tzinfo on read)
    to UTC-aware so arithmetic is consistent."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _freshness_decay(observed_at: datetime, now: datetime) -> float:
    """Exponential decay with one-week half-life, floored at 0.05.

    Floor keeps very old observations from being silently dropped; the
    MAX_FRESHNESS_DAYS filter handles the hard cutoff.
    """
    age_hours = max(0.0, (now - _as_utc(observed_at)).total_seconds() / 3600.0)
    decay = 0.5 ** (age_hours / FRESHNESS_HALFLIFE_HOURS)
    return max(FRESHNESS_FLOOR, decay)


def _weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """Weighted median over (value, weight) pairs. Standard cumulative-weight
    algorithm; ties on the boundary average the two flanking values."""
    if not pairs:
        return None
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    target = total / 2.0
    cum = 0.0
    for i, (v, w) in enumerate(pairs):
        cum += w
        if cum >= target:
            # Exact-boundary refinement: average with previous if cum landed
            # exactly on the half. Negligible practical difference but avoids
            # asymmetry on small samples.
            if abs(cum - target) < 1e-9 and i + 1 < len(pairs):
                return (v + pairs[i + 1][0]) / 2.0
            return v
    return pairs[-1][0]


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = pct * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


# ─────────────────────────────────────────────────────────────────────────────
# Core recompute
# ─────────────────────────────────────────────────────────────────────────────

def _recompute_for_variant(shop_domain: str, shopify_variant_id: str) -> bool:
    """Returns True if a stats row was written (i.e. there's at least one
    qualifying competitor)."""
    now = datetime.now(timezone.utc)

    with get_db() as session:
        rows = session.execute(
            text("""
                SELECT pm."competitorVariantId" AS cvid,
                       pm.confidence            AS conf,
                       c.weight                 AS cw,
                       c.enabled                AS c_enabled,
                       sp.domain                AS dom,
                       cpo.price                AS price,
                       cpo."isInStock"          AS in_stock,
                       cpo."observedAt"         AS observed_at
                FROM "ProductMatch" pm
                LEFT JOIN "ScrapedVariant" sv ON sv.id = pm."competitorVariantId"
                LEFT JOIN "ScrapedProduct" sp ON sp.id = sv."productId"
                LEFT JOIN "Competitor"    c  ON c."shopDomain" = pm."shopDomain"
                                            AND c.domain = sp.domain
                LEFT JOIN LATERAL (
                    SELECT price, "isInStock", "observedAt"
                    FROM "CompetitorPriceObservation"
                    WHERE "competitorVariantId" = pm."competitorVariantId"
                    ORDER BY "observedAt" DESC
                    LIMIT 1
                ) cpo ON TRUE
                WHERE pm."shopifyVariantId" = :v
                  AND pm."dismissedAt" IS NULL
                  AND pm.confidence IS NOT NULL
            """),
            {"v": shopify_variant_id},
        ).all()

        qualifying: list[dict] = []
        confs: list[float] = []
        for r in rows:
            if r.c_enabled is False:
                continue
            if r.in_stock is False:
                continue
            if r.price is None:
                continue
            try:
                price = float(r.price)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            conf = float(r.conf or 0)
            if conf < MIN_USABLE_CONFIDENCE:
                continue
            if r.observed_at is None:
                continue
            age_days = (now - _as_utc(r.observed_at)).total_seconds() / 86400.0
            if age_days > MAX_FRESHNESS_DAYS:
                continue
            cw = float(r.cw) if r.cw is not None else 0.5
            weight = cw * conf * _freshness_decay(r.observed_at, now)
            if weight <= 0:
                continue
            qualifying.append({
                "cvid":   r.cvid,
                "price":  price,
                "weight": weight,
            })
            confs.append(conf)

        if not qualifying:
            # Don't leave a stale stats row around if every competitor was
            # filtered out — that would make the pricing engine think it
            # had data when it doesn't.
            session.execute(
                text('DELETE FROM "VariantCompetitorStats" WHERE "shopifyVariantId" = :v'),
                {"v": shopify_variant_id},
            )
            return False

        prices_sorted = sorted(q["price"] for q in qualifying)
        pairs = [(q["price"], q["weight"]) for q in qualifying]

        # Volatility: stddev/mean of all observations from the matched set
        # in the last 24h. Across competitors, not per-competitor — what
        # matters for pricing is "how unstable is this product's pricing
        # in the market right now?".
        vol_row = session.execute(
            text("""
                SELECT COALESCE(stddev_pop(price), 0) AS s,
                       COALESCE(avg(price), 0)       AS m
                FROM "CompetitorPriceObservation"
                WHERE "competitorVariantId" = ANY(:cvids)
                  AND "observedAt" > NOW() - INTERVAL '24 hours'
            """),
            {"cvids": [q["cvid"] for q in qualifying]},
        ).first()
        volatility_24h = None
        if vol_row and vol_row.m and float(vol_row.m) > 0:
            volatility_24h = float(vol_row.s) / float(vol_row.m)

        stats = {
            "competitorCount":    len(qualifying),
            "minPrice":           prices_sorted[0],
            "p25":                _percentile(prices_sorted, 0.25),
            "median":             _percentile(prices_sorted, 0.50),
            "p75":                _percentile(prices_sorted, 0.75),
            "maxPrice":           prices_sorted[-1],
            "weightedMin":        min(q["price"] for q in qualifying),
            "weightedMedian":     _weighted_median(pairs),
            "volatility24h":      volatility_24h,
            "avgMatchConfidence": statistics.mean(confs),
        }

        session.execute(
            text("""
                INSERT INTO "VariantCompetitorStats" (
                    "shopifyVariantId", "shopDomain",
                    "competitorCount", "minPrice", "p25", "median", "p75", "maxPrice",
                    "weightedMin", "weightedMedian",
                    "volatility24h", "avgMatchConfidence", "lastUpdatedAt"
                ) VALUES (
                    :v, :s,
                    :cnt, :mn, :p25, :med, :p75, :mx,
                    :wmin, :wmed,
                    :vol, :amc, NOW()
                )
                ON CONFLICT ("shopifyVariantId") DO UPDATE SET
                    "competitorCount"    = EXCLUDED."competitorCount",
                    "minPrice"           = EXCLUDED."minPrice",
                    "p25"                = EXCLUDED."p25",
                    "median"             = EXCLUDED."median",
                    "p75"                = EXCLUDED."p75",
                    "maxPrice"           = EXCLUDED."maxPrice",
                    "weightedMin"        = EXCLUDED."weightedMin",
                    "weightedMedian"     = EXCLUDED."weightedMedian",
                    "volatility24h"      = EXCLUDED."volatility24h",
                    "avgMatchConfidence" = EXCLUDED."avgMatchConfidence",
                    "lastUpdatedAt"      = NOW()
            """),
            {
                "v": shopify_variant_id, "s": shop_domain,
                "cnt":  stats["competitorCount"],
                "mn":   round(stats["minPrice"], 2),
                "p25":  round(stats["p25"], 2) if stats["p25"] is not None else None,
                "med":  round(stats["median"], 2) if stats["median"] is not None else None,
                "p75":  round(stats["p75"], 2) if stats["p75"] is not None else None,
                "mx":   round(stats["maxPrice"], 2),
                "wmin": round(stats["weightedMin"], 2),
                "wmed": round(stats["weightedMedian"], 2) if stats["weightedMedian"] is not None else None,
                "vol":  round(volatility_24h, 4) if volatility_24h is not None else None,
                "amc":  round(stats["avgMatchConfidence"], 3),
            },
        )

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Celery tasks
# ─────────────────────────────────────────────────────────────────────────────

@app.task(name="stats.recompute_for_variant", bind=True, max_retries=3, default_retry_delay=30)
def recompute_for_variant(self, shop_domain: str, shopify_variant_id: str):
    """Recompute stats for one merchant variant; queue pricing if rows landed.

    The plan envisions a Redis Streams topic `variant.stats_changed` here;
    using direct Celery enqueue keeps v1 on existing infra. The contract is
    the same: pricing decides exactly once per stats change. Swap later.
    """
    try:
        wrote = _recompute_for_variant(shop_domain, shopify_variant_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            print(f"[stats] variant {shopify_variant_id} permanently failed: {exc}", flush=True)
            return False
        raise self.retry(exc=exc)

    if wrote:
        app.send_task(
            "pricing.decide_for_variant",
            args=[shop_domain, shopify_variant_id],
            queue="pricing_queue",
        )
    return wrote


@app.task(name="stats.recompute_after_observation", bind=True, max_retries=3, default_retry_delay=30)
def recompute_after_observation(self, shop_domain: str, competitor_variant_id: str):
    """Fan out a recompute task for every merchant variant currently matched
    to the given competitor variant. Triggered by the extractor after each
    observation write."""
    try:
        with get_db() as session:
            rows = session.execute(
                text(
                    'SELECT DISTINCT "shopifyVariantId" '
                    'FROM "ProductMatch" '
                    'WHERE "competitorVariantId" = :cv AND "dismissedAt" IS NULL'
                ),
                {"cv": competitor_variant_id},
            ).all()
        for (svid,) in rows:
            app.send_task(
                "stats.recompute_for_variant",
                args=[shop_domain, svid],
                queue="stats_queue",
            )
        return len(rows)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            print(f"[stats] fan-out for {competitor_variant_id} permanently failed: {exc}", flush=True)
            return 0
        raise self.retry(exc=exc)
