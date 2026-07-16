-- AlterTable
ALTER TABLE "ShopifyProduct" ADD COLUMN "dynamicPricingConfiguredAt" TIMESTAMP(3);

-- AlterTable
ALTER TABLE "ShopSettings" ADD COLUMN "defaultPricingTier" "PricingTier" NOT NULL DEFAULT 'COMPETITIVE';

-- Backfill: replicate today's "previously configured" heuristic exactly,
-- so no currently-working paused/active product regresses into being
-- treated as never-configured after this migration. Going forward, the
-- new dynamicPricingConfiguredAt column (set by apply_pane_config) is the
-- maintained signal — this UPDATE runs once, here, and nowhere else.
UPDATE "ShopifyProduct"
SET "dynamicPricingConfiguredAt" = now()
WHERE "dynamicPricingEnabled" = true
   OR "frequencyUnit" IS NOT NULL
   OR "frequencyInterval" IS NOT NULL
   OR EXISTS (
     SELECT 1 FROM "ProductUrl"
     WHERE "ProductUrl"."shopifyProductId" = "ShopifyProduct".id
   );
