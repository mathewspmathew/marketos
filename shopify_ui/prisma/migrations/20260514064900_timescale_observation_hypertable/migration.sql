-- Enable TimescaleDB and convert CompetitorPriceObservation to a hypertable.
-- Hypertable partitions by observedAt for fast time-range queries and
-- continuous aggregates of competitor price stats.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Timescale requires the partitioning column to be part of any unique
-- constraint, including the primary key. Recreate PK as (id, observedAt)
-- before converting to a hypertable.
ALTER TABLE "CompetitorPriceObservation"
  DROP CONSTRAINT IF EXISTS "CompetitorPriceObservation_pkey";

ALTER TABLE "CompetitorPriceObservation"
  ADD CONSTRAINT "CompetitorPriceObservation_pkey"
  PRIMARY KEY (id, "observedAt");

-- chunk_time_interval = 1 day balances query speed vs chunk count.
SELECT create_hypertable(
  '"CompetitorPriceObservation"',
  'observedAt',
  chunk_time_interval => INTERVAL '1 day',
  if_not_exists       => TRUE,
  migrate_data        => TRUE
);
