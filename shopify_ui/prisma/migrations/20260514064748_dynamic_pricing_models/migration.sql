-- CreateEnum
CREATE TYPE "CompetitorTier" AS ENUM ('PREMIUM', 'MIDMARKET', 'BUDGET');

-- CreateEnum
CREATE TYPE "MatchConfidenceTier" AS ENUM ('CONFIRMED', 'LIKELY', 'WEAK');

-- CreateEnum
CREATE TYPE "PricingRuleScope" AS ENUM ('SHOP', 'COLLECTION', 'TAG', 'PRODUCT', 'VARIANT');

-- CreateEnum
CREATE TYPE "PricingRuleType" AS ENUM ('MATCH_LOWEST', 'BEAT_BY_PCT', 'STAY_ABOVE_COST_PCT', 'MATCH_TIER_LOWEST');

-- DropForeignKey
ALTER TABLE "ShopifyEmbedding" DROP CONSTRAINT "ShopifyEmbedding_shopDomain_fkey";

-- DropIndex
DROP INDEX "idx_pe_vector";

-- DropIndex
DROP INDEX "idx_pe_vector_img";

-- DropIndex
DROP INDEX "idx_se_vector";

-- DropIndex
DROP INDEX "idx_se_vector_img";

-- AlterTable
ALTER TABLE "ProductMatch" ADD COLUMN     "confidence" DECIMAL(4,3),
ADD COLUMN     "confidenceTier" "MatchConfidenceTier",
ADD COLUMN     "dismissedAt" TIMESTAMP(3),
ADD COLUMN     "productMatchId" TEXT;

-- AlterTable
ALTER TABLE "ScrapedVariant" ADD COLUMN     "currency" VARCHAR(3) NOT NULL DEFAULT 'INR';

-- AlterTable
ALTER TABLE "ShopifyVariant" ADD COLUMN     "autoPriceEnabled" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "cost" DECIMAL(10,2),
ADD COLUMN     "inventoryQuantity" INTEGER;

-- CreateTable
CREATE TABLE "Competitor" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "domain" TEXT NOT NULL,
    "displayName" TEXT,
    "tier" "CompetitorTier" NOT NULL DEFAULT 'MIDMARKET',
    "weight" DECIMAL(3,2) NOT NULL DEFAULT 0.5,
    "enabled" BOOLEAN NOT NULL DEFAULT true,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "Competitor_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "CompetitorPriceObservation" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "competitorId" TEXT,
    "competitorVariantId" TEXT NOT NULL,
    "price" DECIMAL(10,2) NOT NULL,
    "currency" VARCHAR(3) NOT NULL DEFAULT 'INR',
    "isInStock" BOOLEAN NOT NULL DEFAULT true,
    "observedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "CompetitorPriceObservation_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "VariantCompetitorStats" (
    "shopifyVariantId" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "competitorCount" INTEGER NOT NULL DEFAULT 0,
    "minPrice" DECIMAL(10,2),
    "p25" DECIMAL(10,2),
    "median" DECIMAL(10,2),
    "p75" DECIMAL(10,2),
    "maxPrice" DECIMAL(10,2),
    "weightedMin" DECIMAL(10,2),
    "weightedMedian" DECIMAL(10,2),
    "volatility24h" DECIMAL(6,4),
    "avgMatchConfidence" DECIMAL(4,3),
    "lastUpdatedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "VariantCompetitorStats_pkey" PRIMARY KEY ("shopifyVariantId")
);

-- CreateTable
CREATE TABLE "ProductLevelMatch" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "shopifyProductId" TEXT NOT NULL,
    "scrapedProductId" TEXT NOT NULL,
    "confidence" DECIMAL(4,3) NOT NULL,
    "confidenceTier" "MatchConfidenceTier" NOT NULL,
    "source" TEXT NOT NULL DEFAULT 'RERANKER',
    "confirmedByMerchant" BOOLEAN NOT NULL DEFAULT false,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ProductLevelMatch_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "PricingRule" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "scope" "PricingRuleScope" NOT NULL DEFAULT 'SHOP',
    "scopeRef" TEXT,
    "ruleType" "PricingRuleType" NOT NULL,
    "params" JSONB NOT NULL DEFAULT '{}',
    "floorPrice" DECIMAL(10,2),
    "ceilingPrice" DECIMAL(10,2),
    "maxDailyDeltaPct" DECIMAL(5,2),
    "tierFilter" TEXT[],
    "maxStalenessSeconds" INTEGER NOT NULL DEFAULT 86400,
    "autoApply" BOOLEAN NOT NULL DEFAULT false,
    "priority" INTEGER NOT NULL DEFAULT 100,
    "enabled" BOOLEAN NOT NULL DEFAULT true,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "PricingRule_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "PricingConfig" (
    "shopDomain" TEXT NOT NULL,
    "sparseNudgeAmplitude" DECIMAL(4,3) NOT NULL DEFAULT 0.02,
    "hotVelocityRatio" DECIMAL(4,2) NOT NULL DEFAULT 1.3,
    "coolVelocityRatio" DECIMAL(4,2) NOT NULL DEFAULT 0.7,
    "lowStockDays" INTEGER NOT NULL DEFAULT 7,
    "highStockDays" INTEGER NOT NULL DEFAULT 90,
    "killSwitch" BOOLEAN NOT NULL DEFAULT false,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "PricingConfig_pkey" PRIMARY KEY ("shopDomain")
);

-- CreateTable
CREATE TABLE "PriceDecision" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "shopifyVariantId" TEXT NOT NULL,
    "ruleId" TEXT,
    "oldPrice" DECIMAL(10,2) NOT NULL,
    "newPrice" DECIMAL(10,2) NOT NULL,
    "reason" TEXT NOT NULL,
    "blockedBy" TEXT,
    "confidence" DECIMAL(4,3),
    "statsSnapshot" JSONB,
    "signalsSnapshot" JSONB,
    "decidedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "appliedAt" TIMESTAMP(3),

    CONSTRAINT "PriceDecision_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "PriceChange" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "shopifyVariantId" TEXT NOT NULL,
    "decisionId" TEXT,
    "oldPrice" DECIMAL(10,2) NOT NULL,
    "newPrice" DECIMAL(10,2) NOT NULL,
    "shopifyResponse" JSONB,
    "appliedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "revertedAt" TIMESTAMP(3),

    CONSTRAINT "PriceChange_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "PricingAlert" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "shopifyVariantId" TEXT,
    "type" TEXT NOT NULL,
    "severity" TEXT NOT NULL DEFAULT 'INFO',
    "payload" JSONB,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "dismissedAt" TIMESTAMP(3),

    CONSTRAINT "PricingAlert_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "PromotionWindow" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "scope" "PricingRuleScope" NOT NULL DEFAULT 'SHOP',
    "scopeRef" TEXT,
    "startsAt" TIMESTAMP(3) NOT NULL,
    "endsAt" TIMESTAMP(3) NOT NULL,
    "pauseAutoPricing" BOOLEAN NOT NULL DEFAULT true,
    "note" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "PromotionWindow_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "SalesAggregate" (
    "shopifyVariantId" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "orders7d" INTEGER NOT NULL DEFAULT 0,
    "revenue7d" DECIMAL(12,2) NOT NULL DEFAULT 0,
    "orders28d" INTEGER NOT NULL DEFAULT 0,
    "revenue28d" DECIMAL(12,2) NOT NULL DEFAULT 0,
    "daysOfStock" DECIMAL(8,2),
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "SalesAggregate_pkey" PRIMARY KEY ("shopifyVariantId")
);

-- CreateTable
CREATE TABLE "ShopAuth" (
    "shopDomain" TEXT NOT NULL,
    "offlineAccessToken" TEXT NOT NULL,
    "scopes" TEXT NOT NULL,
    "installedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ShopAuth_pkey" PRIMARY KEY ("shopDomain")
);

-- CreateIndex
CREATE INDEX "Competitor_shopDomain_enabled_idx" ON "Competitor"("shopDomain", "enabled");

-- CreateIndex
CREATE UNIQUE INDEX "Competitor_shopDomain_domain_key" ON "Competitor"("shopDomain", "domain");

-- CreateIndex
CREATE INDEX "CompetitorPriceObservation_competitorVariantId_observedAt_idx" ON "CompetitorPriceObservation"("competitorVariantId", "observedAt" DESC);

-- CreateIndex
CREATE INDEX "CompetitorPriceObservation_shopDomain_observedAt_idx" ON "CompetitorPriceObservation"("shopDomain", "observedAt" DESC);

-- CreateIndex
CREATE INDEX "VariantCompetitorStats_shopDomain_idx" ON "VariantCompetitorStats"("shopDomain");

-- CreateIndex
CREATE INDEX "ProductLevelMatch_shopDomain_confidenceTier_idx" ON "ProductLevelMatch"("shopDomain", "confidenceTier");

-- CreateIndex
CREATE UNIQUE INDEX "ProductLevelMatch_shopifyProductId_scrapedProductId_key" ON "ProductLevelMatch"("shopifyProductId", "scrapedProductId");

-- CreateIndex
CREATE INDEX "PricingRule_shopDomain_enabled_priority_idx" ON "PricingRule"("shopDomain", "enabled", "priority");

-- CreateIndex
CREATE INDEX "PricingRule_shopDomain_scope_scopeRef_idx" ON "PricingRule"("shopDomain", "scope", "scopeRef");

-- CreateIndex
CREATE INDEX "PriceDecision_shopDomain_decidedAt_idx" ON "PriceDecision"("shopDomain", "decidedAt" DESC);

-- CreateIndex
CREATE INDEX "PriceDecision_shopifyVariantId_decidedAt_idx" ON "PriceDecision"("shopifyVariantId", "decidedAt" DESC);

-- CreateIndex
CREATE INDEX "PriceDecision_shopDomain_appliedAt_idx" ON "PriceDecision"("shopDomain", "appliedAt");

-- CreateIndex
CREATE INDEX "PriceChange_shopDomain_appliedAt_idx" ON "PriceChange"("shopDomain", "appliedAt" DESC);

-- CreateIndex
CREATE INDEX "PriceChange_shopifyVariantId_appliedAt_idx" ON "PriceChange"("shopifyVariantId", "appliedAt" DESC);

-- CreateIndex
CREATE INDEX "PricingAlert_shopDomain_dismissedAt_createdAt_idx" ON "PricingAlert"("shopDomain", "dismissedAt", "createdAt" DESC);

-- CreateIndex
CREATE INDEX "PromotionWindow_shopDomain_startsAt_endsAt_idx" ON "PromotionWindow"("shopDomain", "startsAt", "endsAt");

-- CreateIndex
CREATE INDEX "SalesAggregate_shopDomain_idx" ON "SalesAggregate"("shopDomain");

-- CreateIndex
CREATE INDEX "idx_pm_shop_dismissed" ON "ProductMatch"("shopDomain", "dismissedAt");

-- AddForeignKey
ALTER TABLE "ProductMatch" ADD CONSTRAINT "ProductMatch_productMatchId_fkey" FOREIGN KEY ("productMatchId") REFERENCES "ProductLevelMatch"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "Competitor" ADD CONSTRAINT "Competitor_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CompetitorPriceObservation" ADD CONSTRAINT "CompetitorPriceObservation_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CompetitorPriceObservation" ADD CONSTRAINT "CompetitorPriceObservation_competitorId_fkey" FOREIGN KEY ("competitorId") REFERENCES "Competitor"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CompetitorPriceObservation" ADD CONSTRAINT "CompetitorPriceObservation_competitorVariantId_fkey" FOREIGN KEY ("competitorVariantId") REFERENCES "ScrapedVariant"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "VariantCompetitorStats" ADD CONSTRAINT "VariantCompetitorStats_shopifyVariantId_fkey" FOREIGN KEY ("shopifyVariantId") REFERENCES "ShopifyVariant"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductLevelMatch" ADD CONSTRAINT "ProductLevelMatch_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductLevelMatch" ADD CONSTRAINT "ProductLevelMatch_shopifyProductId_fkey" FOREIGN KEY ("shopifyProductId") REFERENCES "ShopifyProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductLevelMatch" ADD CONSTRAINT "ProductLevelMatch_scrapedProductId_fkey" FOREIGN KEY ("scrapedProductId") REFERENCES "ScrapedProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PricingRule" ADD CONSTRAINT "PricingRule_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PricingConfig" ADD CONSTRAINT "PricingConfig_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PriceDecision" ADD CONSTRAINT "PriceDecision_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PriceDecision" ADD CONSTRAINT "PriceDecision_shopifyVariantId_fkey" FOREIGN KEY ("shopifyVariantId") REFERENCES "ShopifyVariant"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PriceDecision" ADD CONSTRAINT "PriceDecision_ruleId_fkey" FOREIGN KEY ("ruleId") REFERENCES "PricingRule"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PriceChange" ADD CONSTRAINT "PriceChange_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PriceChange" ADD CONSTRAINT "PriceChange_shopifyVariantId_fkey" FOREIGN KEY ("shopifyVariantId") REFERENCES "ShopifyVariant"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PriceChange" ADD CONSTRAINT "PriceChange_decisionId_fkey" FOREIGN KEY ("decisionId") REFERENCES "PriceDecision"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PricingAlert" ADD CONSTRAINT "PricingAlert_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PricingAlert" ADD CONSTRAINT "PricingAlert_shopifyVariantId_fkey" FOREIGN KEY ("shopifyVariantId") REFERENCES "ShopifyVariant"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "PromotionWindow" ADD CONSTRAINT "PromotionWindow_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "SalesAggregate" ADD CONSTRAINT "SalesAggregate_shopifyVariantId_fkey" FOREIGN KEY ("shopifyVariantId") REFERENCES "ShopifyVariant"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ShopAuth" ADD CONSTRAINT "ShopAuth_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;
