-- CreateEnum
CREATE TYPE "ActivityEventType" AS ENUM ('COMPETITOR_OBSERVATION', 'DECISION_MADE', 'DECISION_APPLIED', 'DECISION_FAILED', 'DECISION_SKIPPED');

-- CreateTable
CREATE TABLE "ActivityEvent" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "eventType" "ActivityEventType" NOT NULL,
    "occurredAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "summary" TEXT NOT NULL,
    "details" JSONB NOT NULL,
    "competitorVariantId" TEXT,
    "competitorProductId" TEXT,
    "competitorPrice" DECIMAL(10,2),
    "competitorIsInStock" BOOLEAN,
    "priceDecisionId" TEXT,
    "shopifyProductId" TEXT,
    "shopifyVariantId" TEXT,
    "oldPrice" DECIMAL(10,2),
    "newPrice" DECIMAL(10,2),
    "changePct" DOUBLE PRECISION,
    "refPrice" DECIMAL(10,2),
    "competitorsUsed" INTEGER,
    "tierAtDecision" "PricingTier",
    "topMatchesJson" JSONB,
    "applyError" TEXT,
    "skipReason" TEXT,

    CONSTRAINT "ActivityEvent_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "ActivityEvent_shopDomain_occurredAt_idx" ON "ActivityEvent"("shopDomain", "occurredAt" DESC);

-- CreateIndex
CREATE INDEX "ActivityEvent_shopifyProductId_occurredAt_idx" ON "ActivityEvent"("shopifyProductId", "occurredAt" DESC);

-- CreateIndex
CREATE INDEX "ActivityEvent_eventType_occurredAt_idx" ON "ActivityEvent"("eventType", "occurredAt" DESC);

-- AddForeignKey
ALTER TABLE "ActivityEvent" ADD CONSTRAINT "ActivityEvent_competitorVariantId_fkey" FOREIGN KEY ("competitorVariantId") REFERENCES "ScrapedVariant"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ActivityEvent" ADD CONSTRAINT "ActivityEvent_competitorProductId_fkey" FOREIGN KEY ("competitorProductId") REFERENCES "ScrapedProduct"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ActivityEvent" ADD CONSTRAINT "ActivityEvent_shopifyProductId_fkey" FOREIGN KEY ("shopifyProductId") REFERENCES "ShopifyProduct"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ActivityEvent" ADD CONSTRAINT "ActivityEvent_shopifyVariantId_fkey" FOREIGN KEY ("shopifyVariantId") REFERENCES "ShopifyVariant"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ActivityEvent" ADD CONSTRAINT "ActivityEvent_priceDecisionId_fkey" FOREIGN KEY ("priceDecisionId") REFERENCES "PriceDecision"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ActivityEvent" ADD CONSTRAINT "ActivityEvent_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;
