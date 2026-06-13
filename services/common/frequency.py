"""
services/common/frequency.py

Shared rescrape-cadence helpers.

UI contract: the frequency unit is a dropdown with three fixed options —
"minute", "hour", "day" — and the interval is a positive integer. Anything
the user can submit is validated against `CANONICAL_UNITS` before write.

Read path tolerates a small set of historical aliases ("hourly", "daily",
"weekly", "monthly") so legacy ScrapingConfig rows still resolve until
they're migrated away.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────────────────────────────────────
# UI-facing canonical options. Surfaced via API so the frontend renders the
# exact same string set the backend validates against — no drift.
# ─────────────────────────────────────────────────────────────────────────────
CANONICAL_UNITS: tuple[str, ...] = ("never", "minute", "hour", "day")

# Friendly labels for the dropdown. Order matches CANONICAL_UNITS.
UNIT_LABELS: dict[str, str] = {
    "never":  "Never (one-time discovery)",
    "minute": "Minutes",
    "hour":   "Hours",
    "day":    "Days",
}

# Sensible default interval per unit, used when only the unit changes.
# "never" has no interval — discovery still runs once but no rescrape loop.
DEFAULT_INTERVAL: dict[str, int | None] = {
    "never":  None,
    "minute": 15,
    "hour":   6,
    "day":    1,
}

# Lower bound. Sub-minute polling just gets the merchant rate-limited.
MIN_INTERVAL = timedelta(minutes=1)


class InvalidFrequency(ValueError):
    """Raised by validate_unit / validate_interval on bad input."""


def validate_unit(unit: str | None) -> str:
    """Return the canonical unit string or raise InvalidFrequency.

    Use this on every write path (form submit, webhook, API). Read paths
    should use `resolve_delta` instead — it tolerates legacy aliases.
    """
    norm = (unit or "").strip().lower()
    if norm not in CANONICAL_UNITS:
        raise InvalidFrequency(
            f"frequencyUnit must be one of {CANONICAL_UNITS}, got {unit!r}"
        )
    return norm


def validate_interval(interval: int | None) -> int:
    if interval is None or int(interval) <= 0:
        raise InvalidFrequency(f"frequencyInterval must be a positive int, got {interval!r}")
    return int(interval)


# ─────────────────────────────────────────────────────────────────────────────
# Read-side: lenient mapping. Includes legacy aliases for old data.
# ─────────────────────────────────────────────────────────────────────────────
_UNIT_TO_DELTA: dict[str, timedelta] = {
    # canonical (UI ships these)
    "minute":  timedelta(minutes=1),
    "hour":    timedelta(hours=1),
    "day":     timedelta(days=1),
    # legacy aliases — accepted at READ time only.
    "min":     timedelta(minutes=1),
    "mins":    timedelta(minutes=1),
    "minutes": timedelta(minutes=1),
    "hr":      timedelta(hours=1),
    "hrs":     timedelta(hours=1),
    "hours":   timedelta(hours=1),
    "hourly":  timedelta(hours=1),
    "days":    timedelta(days=1),
    "daily":   timedelta(days=1),
    "weekly":  timedelta(weeks=1),
    "monthly": timedelta(days=30),
}


def resolve_delta(
    interval: int | None,
    unit: str | None,
    *,
    default: timedelta = timedelta(days=1),
) -> timedelta | None:
    """Turn (interval, unit) into a timedelta. Unknown/null inputs use `default`.

    Returns None when the unit is "never" — caller should treat that as
    "no recurring schedule" (one-time discovery + scrape).
    """
    norm = (unit or "").strip().lower()
    if norm == "never":
        return None
    if not interval or int(interval) <= 0:
        return default
    base = _UNIT_TO_DELTA.get(norm)
    if base is None:
        return default
    delta = base * int(interval)
    return delta if delta >= MIN_INTERVAL else MIN_INTERVAL


def next_run_at(
    interval: int | None,
    unit: str | None,
    *,
    default: timedelta = timedelta(days=1),
) -> datetime | None:
    """Absolute UTC timestamp for the next rescrape, or None if unit is 'never'."""
    delta = resolve_delta(interval, unit, default=default)
    if delta is None:
        return None
    return datetime.now(timezone.utc) + delta
