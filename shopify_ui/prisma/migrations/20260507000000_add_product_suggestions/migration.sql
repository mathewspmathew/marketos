-- Suggestion status enum (shared by both suggestion tables)
CREATE TYPE "SuggestionStatus" AS ENUM ('FIRST_TIME', 'SHOWED', 'SKIPPED', 'APPLIED');

-- Product-level: LLM-generated title + description suggestions
CREATE TABLE "ProductSuggestion" (
    "id"                       TEXT NOT NULL,
    "shopDomain"               TEXT NOT NULL,
    "shopifyProductId"         TEXT NOT NULL,

    "suggestedTitle"           TEXT,
    "suggestedDescriptionHtml" TEXT,
    "contentRationale"         TEXT,

    "editedTitle"              TEXT,
    "editedDescriptionHtml"    TEXT,

    "appliedTitle"             TEXT,
    "appliedDescriptionHtml"   TEXT,

    "matchCount"               INTEGER          NOT NULL DEFAULT 0,
    "avgMatchScore"            DECIMAL(5,2),

    "status"                   "SuggestionStatus" NOT NULL DEFAULT 'FIRST_TIME',
    "generatedAt"              TIMESTAMP(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt"                TIMESTAMP(3)     NOT NULL,
    "appliedAt"                TIMESTAMP(3),

    CONSTRAINT "ProductSuggestion_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "ProductSuggestion_shopifyProductId_key"
  ON "ProductSuggestion"("shopifyProductId");

CREATE INDEX "ProductSuggestion_shopDomain_status_idx"
  ON "ProductSuggestion"("shopDomain", "status");

ALTER TABLE "ProductSuggestion"
  ADD CONSTRAINT "ProductSuggestion_shopifyProductId_fkey"
  FOREIGN KEY ("shopifyProductId") REFERENCES "ShopifyProduct"("id")
  ON DELETE CASCADE ON UPDATE CASCADE;

-- Variant-level: competitor price aggregates + user-chosen price
CREATE TABLE "VariantPriceSuggestion" (
    "id"               TEXT NOT NULL,
    "shopDomain"       TEXT NOT NULL,
    "shopifyVariantId" TEXT NOT NULL,

    "competitorMin"    DECIMAL(10,2),
    "competitorMedian" DECIMAL(10,2),
    "competitorMax"    DECIMAL(10,2),
    "competitorCount"  INTEGER          NOT NULL DEFAULT 0,
    "priceRationale"   TEXT,

    "chosenPrice"      DECIMAL(10,2),
    "appliedPrice"     DECIMAL(10,2),

    "status"           "SuggestionStatus" NOT NULL DEFAULT 'FIRST_TIME',
    "generatedAt"      TIMESTAMP(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt"        TIMESTAMP(3)     NOT NULL,
    "appliedAt"        TIMESTAMP(3),

    CONSTRAINT "VariantPriceSuggestion_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "VariantPriceSuggestion_shopifyVariantId_key"
  ON "VariantPriceSuggestion"("shopifyVariantId");

CREATE INDEX "VariantPriceSuggestion_shopDomain_status_idx"
  ON "VariantPriceSuggestion"("shopDomain", "status");

ALTER TABLE "VariantPriceSuggestion"
  ADD CONSTRAINT "VariantPriceSuggestion_shopifyVariantId_fkey"
  FOREIGN KEY ("shopifyVariantId") REFERENCES "ShopifyVariant"("id")
  ON DELETE CASCADE ON UPDATE CASCADE;
