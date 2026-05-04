-- AlterTable: move semanticText from ShopifyEmbedding to ShopifyVariant
ALTER TABLE "ShopifyVariant" ADD COLUMN "semanticText" TEXT;

-- Migrate existing data before dropping the source column
UPDATE "ShopifyVariant" sv
SET "semanticText" = se."semanticText"
FROM "ShopifyEmbedding" se
WHERE se."variantId" = sv."id"
  AND se."semanticText" IS NOT NULL;

-- AlterTable
ALTER TABLE "ShopifyEmbedding" DROP COLUMN "semanticText";
