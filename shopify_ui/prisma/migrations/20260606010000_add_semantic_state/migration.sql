ALTER TABLE "ShopifyProduct" ADD COLUMN "semanticStatus" TEXT NOT NULL DEFAULT 'PENDING';
ALTER TABLE "ShopifyProduct" ADD COLUMN "semanticClaimedAt" TIMESTAMPTZ;
ALTER TABLE "ShopifyProduct" ADD COLUMN "semanticVersion" INTEGER NOT NULL DEFAULT 0;
ALTER TABLE "ShopifyProduct" ADD COLUMN "semanticAttempts" INTEGER NOT NULL DEFAULT 0;
ALTER TABLE "ShopifyProduct" ADD COLUMN "semanticFailureReason" TEXT;

-- Seed: complete products -> DONE, everything else stays PENDING (the default).
UPDATE "ShopifyProduct" sp SET "semanticStatus" = 'DONE'
 WHERE ("searchQuery" IS NOT NULL OR "searchQueryOverride" IS NOT NULL)
   AND NOT EXISTS (
     SELECT 1 FROM "ShopifyVariant" v
      WHERE v."productId" = sp.id AND v."semanticText" IS NULL
   );
