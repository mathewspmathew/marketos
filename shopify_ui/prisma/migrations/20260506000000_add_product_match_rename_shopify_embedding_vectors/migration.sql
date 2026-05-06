-- Rename ShopifyEmbedding vector columns to align with ProductEmbedding (vectorText / vectorImg)
ALTER TABLE "ShopifyEmbedding" RENAME COLUMN "textEmbedding"  TO "vectorText";
ALTER TABLE "ShopifyEmbedding" RENAME COLUMN "imageEmbedding" TO "vectorImg";

-- Add denormalized shopDomain for cheap tenant filtering on HNSW queries
ALTER TABLE "ShopifyEmbedding" ADD COLUMN "shopDomain" TEXT;

UPDATE "ShopifyEmbedding" se
SET "shopDomain" = sp."shopDomain"
FROM "ShopifyVariant" sv
JOIN "ShopifyProduct" sp ON sp."id" = sv."productId"
WHERE sv."id" = se."variantId";

ALTER TABLE "ShopifyEmbedding" ALTER COLUMN "shopDomain" SET NOT NULL;

ALTER TABLE "ShopifyEmbedding"
  ADD CONSTRAINT "ShopifyEmbedding_shopDomain_fkey"
  FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

CREATE INDEX "ShopifyEmbedding_shopDomain_idx" ON "ShopifyEmbedding"("shopDomain");

-- HNSW indexes for cosine similarity search
CREATE INDEX IF NOT EXISTS "idx_pe_vector" ON "ProductEmbedding"
  USING hnsw ("vectorText" vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS "idx_se_vector" ON "ShopifyEmbedding"
  USING hnsw ("vectorText" vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ProductMatch: merchant variant ↔ competitor variant similarity rows
CREATE TABLE "ProductMatch" (
    "id"                  TEXT NOT NULL,
    "shopDomain"          TEXT NOT NULL,
    "shopifyVariantId"    TEXT NOT NULL,
    "competitorVariantId" TEXT,
    "competitorProdId"    TEXT NOT NULL,
    "matchScore"          DECIMAL(5,2)  NOT NULL,
    "matchType"           TEXT          NOT NULL DEFAULT 'semantic',
    "vectorDistance"      DECIMAL(10,6) NOT NULL,
    "thresholdUsed"       DECIMAL(5,4)  NOT NULL,
    "matchedAt"           TIMESTAMP(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt"           TIMESTAMP(3)  NOT NULL,

    CONSTRAINT "ProductMatch_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "ProductMatch_shopifyVariantId_competitorVariantId_key"
  ON "ProductMatch"("shopifyVariantId", "competitorVariantId");

CREATE INDEX "idx_pm_shopify"     ON "ProductMatch"("shopifyVariantId", "matchScore" DESC);
CREATE INDEX "idx_pm_competitor"  ON "ProductMatch"("competitorVariantId");
CREATE INDEX "idx_pm_shop_domain" ON "ProductMatch"("shopDomain", "matchScore" DESC);

ALTER TABLE "ProductMatch"
  ADD CONSTRAINT "ProductMatch_shopDomain_fkey"
  FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

ALTER TABLE "ProductMatch"
  ADD CONSTRAINT "ProductMatch_shopifyVariantId_fkey"
  FOREIGN KEY ("shopifyVariantId") REFERENCES "ShopifyVariant"("id") ON DELETE CASCADE ON UPDATE CASCADE;

ALTER TABLE "ProductMatch"
  ADD CONSTRAINT "ProductMatch_competitorVariantId_fkey"
  FOREIGN KEY ("competitorVariantId") REFERENCES "ScrapedVariant"("id") ON DELETE SET NULL ON UPDATE CASCADE;

ALTER TABLE "ProductMatch"
  ADD CONSTRAINT "ProductMatch_competitorProdId_fkey"
  FOREIGN KEY ("competitorProdId") REFERENCES "ScrapedProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;
