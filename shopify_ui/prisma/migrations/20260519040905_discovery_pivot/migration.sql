/*
  Warnings:

  - You are about to drop the column `tier` on the `Competitor` table. All the data in the column will be lost.
  - You are about to drop the column `weight` on the `Competitor` table. All the data in the column will be lost.
  - You are about to drop the column `alertDismissedAt` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `alertReason` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `alertSeverity` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `blockedBy` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `confidence` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `mlConfidence` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `mlSuggestedPrice` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `modelVersion` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `revertedAt` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `ruleId` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `ruleSuggestedPrice` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `signalsSnapshot` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `statsSnapshot` on the `PriceDecision` table. All the data in the column will be lost.
  - You are about to drop the column `nextScrapAt` on the `ProductUrl` table. All the data in the column will be lost.
  - You are about to drop the column `useMlSuggestion` on the `ShopifyVariant` table. All the data in the column will be lost.
  - You are about to drop the column `avgMatchConfidence` on the `VariantCompetitorStats` table. All the data in the column will be lost.
  - You are about to drop the column `p25` on the `VariantCompetitorStats` table. All the data in the column will be lost.
  - You are about to drop the column `p75` on the `VariantCompetitorStats` table. All the data in the column will be lost.
  - You are about to drop the column `volatility24h` on the `VariantCompetitorStats` table. All the data in the column will be lost.
  - You are about to drop the column `weightedMedian` on the `VariantCompetitorStats` table. All the data in the column will be lost.
  - You are about to drop the column `weightedMin` on the `VariantCompetitorStats` table. All the data in the column will be lost.
  - You are about to drop the `PricingConfig` table. If the table is not empty, all the data it contains will be lost.
  - You are about to drop the `PricingRule` table. If the table is not empty, all the data it contains will be lost.
  - You are about to drop the `PromotionWindow` table. If the table is not empty, all the data it contains will be lost.
  - You are about to drop the `SalesAggregate` table. If the table is not empty, all the data it contains will be lost.

*/
-- CreateEnum
CREATE TYPE "CandidateStatus" AS ENUM ('PENDING', 'VERIFIED', 'REJECTED', 'DEAD');

-- CreateEnum
CREATE TYPE "DiscoveryStatus" AS ENUM ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED');

-- DropForeignKey
ALTER TABLE "PriceDecision" DROP CONSTRAINT "PriceDecision_ruleId_fkey";

-- DropForeignKey
ALTER TABLE "PricingConfig" DROP CONSTRAINT "PricingConfig_shopDomain_fkey";

-- DropForeignKey
ALTER TABLE "PricingRule" DROP CONSTRAINT "PricingRule_shopDomain_fkey";

-- DropForeignKey
ALTER TABLE "ProductUrl" DROP CONSTRAINT "ProductUrl_configId_fkey";

-- DropForeignKey
ALTER TABLE "PromotionWindow" DROP CONSTRAINT "PromotionWindow_shopDomain_fkey";

-- DropForeignKey
ALTER TABLE "SalesAggregate" DROP CONSTRAINT "SalesAggregate_shopifyVariantId_fkey";

-- DropForeignKey
ALTER TABLE "ScrapingError" DROP CONSTRAINT "ScrapingError_configId_fkey";

-- DropIndex
DROP INDEX "CompetitorPriceObservation_observedAt_idx";

-- DropIndex
DROP INDEX "PriceDecision_shopDomain_alertDismissedAt_decidedAt_idx";

-- AlterTable
ALTER TABLE "Competitor" DROP COLUMN "tier",
DROP COLUMN "weight";

-- AlterTable
ALTER TABLE "PriceDecision" DROP COLUMN "alertDismissedAt",
DROP COLUMN "alertReason",
DROP COLUMN "alertSeverity",
DROP COLUMN "blockedBy",
DROP COLUMN "confidence",
DROP COLUMN "mlConfidence",
DROP COLUMN "mlSuggestedPrice",
DROP COLUMN "modelVersion",
DROP COLUMN "revertedAt",
DROP COLUMN "ruleId",
DROP COLUMN "ruleSuggestedPrice",
DROP COLUMN "signalsSnapshot",
DROP COLUMN "statsSnapshot";

-- AlterTable
ALTER TABLE "ProductLevelMatch" ALTER COLUMN "source" SET DEFAULT 'DISCOVERY';

-- AlterTable
ALTER TABLE "ProductUrl" DROP COLUMN "nextScrapAt",
ADD COLUMN     "frequencyInterval" INTEGER,
ADD COLUMN     "frequencyUnit" TEXT DEFAULT 'daily',
ADD COLUMN     "nextRunAt" TIMESTAMP(3),
ADD COLUMN     "shopifyProductId" TEXT,
ALTER COLUMN "configId" DROP NOT NULL;

-- AlterTable
ALTER TABLE "ScrapingError" ALTER COLUMN "configId" DROP NOT NULL;

-- AlterTable
ALTER TABLE "ShopifyProduct" ADD COLUMN     "competitorTrackingEnabled" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "lastDiscoveryAt" TIMESTAMP(3);

-- AlterTable
ALTER TABLE "ShopifyVariant" DROP COLUMN "useMlSuggestion",
ADD COLUMN     "ceilingPrice" DECIMAL(10,2),
ADD COLUMN     "floorPrice" DECIMAL(10,2);

-- AlterTable
ALTER TABLE "VariantCompetitorStats" DROP COLUMN "avgMatchConfidence",
DROP COLUMN "p25",
DROP COLUMN "p75",
DROP COLUMN "volatility24h",
DROP COLUMN "weightedMedian",
DROP COLUMN "weightedMin";

-- DropTable
DROP TABLE "PricingConfig";

-- DropTable
DROP TABLE "PricingRule";

-- DropTable
DROP TABLE "PromotionWindow";

-- DropTable
DROP TABLE "SalesAggregate";

-- DropEnum
DROP TYPE "CompetitorTier";

-- DropEnum
DROP TYPE "PricingRuleScope";

-- DropEnum
DROP TYPE "PricingRuleType";

-- CreateTable
CREATE TABLE "DiscoveryJob" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "shopifyProductId" TEXT NOT NULL,
    "status" "DiscoveryStatus" NOT NULL DEFAULT 'QUEUED',
    "candidateCount" INTEGER NOT NULL DEFAULT 0,
    "error" TEXT,
    "requestedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "completedAt" TIMESTAMP(3),

    CONSTRAINT "DiscoveryJob_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "CompetitorCandidate" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "shopifyProductId" TEXT NOT NULL,
    "discoveryJobId" TEXT,
    "url" TEXT NOT NULL,
    "domain" TEXT NOT NULL,
    "source" TEXT NOT NULL,
    "serpTitle" TEXT,
    "serpSnippet" TEXT,
    "serpPrice" DECIMAL(10,2),
    "rerankScore" DECIMAL(4,3),
    "rerankReason" TEXT,
    "status" "CandidateStatus" NOT NULL DEFAULT 'PENDING',
    "rejectReason" TEXT,
    "scrapedProductId" TEXT,
    "discoveredAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "scrapedAt" TIMESTAMP(3),
    "verifiedAt" TIMESTAMP(3),

    CONSTRAINT "CompetitorCandidate_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ShopSettings" (
    "shopDomain" TEXT NOT NULL,
    "markupPct" DECIMAL(5,4) NOT NULL DEFAULT 0.02,
    "minCompetitorsRequired" INTEGER NOT NULL DEFAULT 2,
    "marketplaceBlocklist" TEXT[] DEFAULT ARRAY[]::TEXT[],
    "killSwitch" BOOLEAN NOT NULL DEFAULT false,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ShopSettings_pkey" PRIMARY KEY ("shopDomain")
);

-- CreateIndex
CREATE INDEX "DiscoveryJob_shopDomain_status_idx" ON "DiscoveryJob"("shopDomain", "status");

-- CreateIndex
CREATE INDEX "DiscoveryJob_shopifyProductId_requestedAt_idx" ON "DiscoveryJob"("shopifyProductId", "requestedAt" DESC);

-- CreateIndex
CREATE INDEX "CompetitorCandidate_shopDomain_status_idx" ON "CompetitorCandidate"("shopDomain", "status");

-- CreateIndex
CREATE UNIQUE INDEX "CompetitorCandidate_shopifyProductId_url_key" ON "CompetitorCandidate"("shopifyProductId", "url");

-- CreateIndex
CREATE INDEX "ProductUrl_shopDomain_status_nextRunAt_idx" ON "ProductUrl"("shopDomain", "status", "nextRunAt");

-- CreateIndex
CREATE INDEX "ProductUrl_shopifyProductId_idx" ON "ProductUrl"("shopifyProductId");

-- AddForeignKey
ALTER TABLE "ProductUrl" ADD CONSTRAINT "ProductUrl_shopifyProductId_fkey" FOREIGN KEY ("shopifyProductId") REFERENCES "ShopifyProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductUrl" ADD CONSTRAINT "ProductUrl_configId_fkey" FOREIGN KEY ("configId") REFERENCES "ScrapingConfig"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ScrapingError" ADD CONSTRAINT "ScrapingError_configId_fkey" FOREIGN KEY ("configId") REFERENCES "ScrapingConfig"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "DiscoveryJob" ADD CONSTRAINT "DiscoveryJob_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "DiscoveryJob" ADD CONSTRAINT "DiscoveryJob_shopifyProductId_fkey" FOREIGN KEY ("shopifyProductId") REFERENCES "ShopifyProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CompetitorCandidate" ADD CONSTRAINT "CompetitorCandidate_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CompetitorCandidate" ADD CONSTRAINT "CompetitorCandidate_shopifyProductId_fkey" FOREIGN KEY ("shopifyProductId") REFERENCES "ShopifyProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CompetitorCandidate" ADD CONSTRAINT "CompetitorCandidate_discoveryJobId_fkey" FOREIGN KEY ("discoveryJobId") REFERENCES "DiscoveryJob"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "CompetitorCandidate" ADD CONSTRAINT "CompetitorCandidate_scrapedProductId_fkey" FOREIGN KEY ("scrapedProductId") REFERENCES "ScrapedProduct"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ShopSettings" ADD CONSTRAINT "ShopSettings_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;
