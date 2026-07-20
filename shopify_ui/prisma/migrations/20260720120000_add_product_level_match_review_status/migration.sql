-- CreateEnum
CREATE TYPE "MatchReviewStatus" AS ENUM ('PENDING', 'CONFIRMED', 'REJECTED');

-- AlterTable
ALTER TABLE "ProductLevelMatch" ADD COLUMN "reviewStatus" "MatchReviewStatus" NOT NULL DEFAULT 'PENDING';

-- Backfill: existing rows have this invariant — every write site that has
-- ever touched confirmedByMerchant/rejectedByMerchant clears the other flag
-- when setting one, so at most one of the two is ever true on any row.
-- REJECTED takes precedence in this CASE as a defensive tie-break in case
-- that invariant is ever violated by data written outside this migration's
-- observation window.
UPDATE "ProductLevelMatch"
SET "reviewStatus" = CASE
  WHEN "rejectedByMerchant"  = true THEN 'REJECTED'::"MatchReviewStatus"
  WHEN "confirmedByMerchant" = true THEN 'CONFIRMED'::"MatchReviewStatus"
  ELSE 'PENDING'::"MatchReviewStatus"
END;
