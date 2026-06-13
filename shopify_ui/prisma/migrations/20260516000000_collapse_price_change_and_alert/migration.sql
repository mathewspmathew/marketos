-- Collapse PriceChange + PricingAlert into PriceDecision.
--
-- A PriceChange row was just the side-effect record of writing a decision to
-- Shopify; fold it into PriceDecision via shopifyResponse + revertedAt.
-- (decidedAt + appliedAt + newPrice already carry the rest.)
--
-- A PricingAlert row was a flagged decision (guardrail block, low confidence,
-- competitor drop, ...); fold via alertReason + alertSeverity + alertDismissedAt.
-- A non-null alertReason means the decision surfaces in the alerts feed.

ALTER TABLE "PriceDecision"
  ADD COLUMN "shopifyResponse" JSONB,
  ADD COLUMN "revertedAt"      TIMESTAMP(3),
  ADD COLUMN "alertReason"     TEXT,
  ADD COLUMN "alertSeverity"   TEXT,
  ADD COLUMN "alertDismissedAt" TIMESTAMP(3);

CREATE INDEX "PriceDecision_shopDomain_alertDismissedAt_decidedAt_idx"
  ON "PriceDecision" ("shopDomain", "alertDismissedAt", "decidedAt" DESC);

DROP TABLE IF EXISTS "PriceChange";
DROP TABLE IF EXISTS "PricingAlert";
