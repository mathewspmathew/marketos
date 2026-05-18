-- Merchant review workflow: track rejection (so we never re-ask) and the
-- timestamp of the merchant's decision (approve OR reject).

ALTER TABLE "ProductLevelMatch"
  ADD COLUMN IF NOT EXISTS "rejectedByMerchant" BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS "reviewedAt"         TIMESTAMP(3);

CREATE INDEX IF NOT EXISTS "ProductLevelMatch_shopDomain_reviewedAt_idx"
  ON "ProductLevelMatch" ("shopDomain", "reviewedAt");
