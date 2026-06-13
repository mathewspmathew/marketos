-- Drop the FK from CompetitorPriceObservation to Competitor before
-- removing the column and the Competitor table.
ALTER TABLE "CompetitorPriceObservation"
  DROP CONSTRAINT IF EXISTS "CompetitorPriceObservation_competitorId_fkey";

ALTER TABLE "CompetitorPriceObservation"
  DROP COLUMN IF EXISTS "competitorId";

DROP TABLE IF EXISTS "Competitor";

DROP TABLE IF EXISTS "ScrapingError";
