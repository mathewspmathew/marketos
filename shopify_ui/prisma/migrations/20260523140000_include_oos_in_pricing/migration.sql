-- Merchant toggle: count out-of-stock competitor observations in pricing decisions.
-- Default FALSE preserves the historical "drop OOS" behavior.

ALTER TABLE "ShopSettings"
    ADD COLUMN "includeOosInPricing" BOOLEAN NOT NULL DEFAULT FALSE;
