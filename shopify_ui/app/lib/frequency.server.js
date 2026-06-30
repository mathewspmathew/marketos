const UNIT_MS = {
  minute: 60_000,
  hour:   3_600_000,
  day:    86_400_000,
};
const MIN_INTERVAL_MS = 60_000;

export function computeNextRunAt(interval, unit) {
  if (!unit || unit === "never") return null;
  const ms = UNIT_MS[unit.toLowerCase()];
  if (!ms || !interval || interval <= 0) return null;
  const delta = Math.max(interval * ms, MIN_INTERVAL_MS);
  return new Date(Date.now() + delta);
}
