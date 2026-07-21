-- Convert all naive TIMESTAMP(3) columns to TIMESTAMPTZ(3).
-- Existing values were always written as UTC-aware datetimes from Python
-- (services/common/models.py uses DateTime(timezone=True) + datetime.now(timezone.utc)),
-- and the DB session timezone is GMT, so reinterpreting the stored naive value as UTC
-- is a lossless, non-shifting conversion.

ALTER TABLE "ChatMessage" ALTER COLUMN "createdAt" TYPE TIMESTAMPTZ(3) USING "createdAt" AT TIME ZONE 'UTC';
ALTER TABLE "ChatPreview" ALTER COLUMN "expiresAt" TYPE TIMESTAMPTZ(3) USING "expiresAt" AT TIME ZONE 'UTC';
ALTER TABLE "ChatPreview" ALTER COLUMN "appliedAt" TYPE TIMESTAMPTZ(3) USING "appliedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ChatPreview" ALTER COLUMN "createdAt" TYPE TIMESTAMPTZ(3) USING "createdAt" AT TIME ZONE 'UTC';
ALTER TABLE "ChatSession" ALTER COLUMN "createdAt" TYPE TIMESTAMPTZ(3) USING "createdAt" AT TIME ZONE 'UTC';
ALTER TABLE "ChatSession" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "CompetitorCandidate" ALTER COLUMN "discoveredAt" TYPE TIMESTAMPTZ(3) USING "discoveredAt" AT TIME ZONE 'UTC';
ALTER TABLE "CompetitorCandidate" ALTER COLUMN "scrapedAt" TYPE TIMESTAMPTZ(3) USING "scrapedAt" AT TIME ZONE 'UTC';
ALTER TABLE "CompetitorCandidate" ALTER COLUMN "verifiedAt" TYPE TIMESTAMPTZ(3) USING "verifiedAt" AT TIME ZONE 'UTC';
ALTER TABLE "CompetitorPriceObservation" ALTER COLUMN "observedAt" TYPE TIMESTAMPTZ(3) USING "observedAt" AT TIME ZONE 'UTC';
ALTER TABLE "DiscoveryJob" ALTER COLUMN "requestedAt" TYPE TIMESTAMPTZ(3) USING "requestedAt" AT TIME ZONE 'UTC';
ALTER TABLE "DiscoveryJob" ALTER COLUMN "completedAt" TYPE TIMESTAMPTZ(3) USING "completedAt" AT TIME ZONE 'UTC';
ALTER TABLE "PriceDecision" ALTER COLUMN "decidedAt" TYPE TIMESTAMPTZ(3) USING "decidedAt" AT TIME ZONE 'UTC';
ALTER TABLE "PriceDecision" ALTER COLUMN "appliedAt" TYPE TIMESTAMPTZ(3) USING "appliedAt" AT TIME ZONE 'UTC';
ALTER TABLE "PriceDecision" ALTER COLUMN "revertedAt" TYPE TIMESTAMPTZ(3) USING "revertedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductEmbedding" ALTER COLUMN "vectorizedAt" TYPE TIMESTAMPTZ(3) USING "vectorizedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductEmbedding" ALTER COLUMN "matchedAt" TYPE TIMESTAMPTZ(3) USING "matchedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductLevelMatch" ALTER COLUMN "createdAt" TYPE TIMESTAMPTZ(3) USING "createdAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductLevelMatch" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductLevelMatch" ALTER COLUMN "reviewedAt" TYPE TIMESTAMPTZ(3) USING "reviewedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductMatch" ALTER COLUMN "matchedAt" TYPE TIMESTAMPTZ(3) USING "matchedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductMatch" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductMatch" ALTER COLUMN "dismissedAt" TYPE TIMESTAMPTZ(3) USING "dismissedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductUrl" ALTER COLUMN "lastScrapedAt" TYPE TIMESTAMPTZ(3) USING "lastScrapedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductUrl" ALTER COLUMN "createdAt" TYPE TIMESTAMPTZ(3) USING "createdAt" AT TIME ZONE 'UTC';
ALTER TABLE "ProductUrl" ALTER COLUMN "nextRunAt" TYPE TIMESTAMPTZ(3) USING "nextRunAt" AT TIME ZONE 'UTC';
ALTER TABLE "ScrapedProduct" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ScrapedProduct" ALTER COLUMN "createdAt" TYPE TIMESTAMPTZ(3) USING "createdAt" AT TIME ZONE 'UTC';
ALTER TABLE "ScrapedVariant" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ScrapedVariant" ALTER COLUMN "createdAt" TYPE TIMESTAMPTZ(3) USING "createdAt" AT TIME ZONE 'UTC';
ALTER TABLE "ScrapingConfig" ALTER COLUMN "nextRunAt" TYPE TIMESTAMPTZ(3) USING "nextRunAt" AT TIME ZONE 'UTC';
ALTER TABLE "ScrapingConfig" ALTER COLUMN "createdAt" TYPE TIMESTAMPTZ(3) USING "createdAt" AT TIME ZONE 'UTC';
ALTER TABLE "ScrapingConfig" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "Session" ALTER COLUMN "expires" TYPE TIMESTAMPTZ(3) USING "expires" AT TIME ZONE 'UTC';
ALTER TABLE "Session" ALTER COLUMN "refreshTokenExpires" TYPE TIMESTAMPTZ(3) USING "refreshTokenExpires" AT TIME ZONE 'UTC';
ALTER TABLE "ShopSettings" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyEmbedding" ALTER COLUMN "embeddedAt" TYPE TIMESTAMPTZ(3) USING "embeddedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyEmbedding" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyEmbedding" ALTER COLUMN "matchedAt" TYPE TIMESTAMPTZ(3) USING "matchedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyProduct" ALTER COLUMN "createdAt" TYPE TIMESTAMPTZ(3) USING "createdAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyProduct" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyProduct" ALTER COLUMN "dynamicPricingConfiguredAt" TYPE TIMESTAMPTZ(3) USING "dynamicPricingConfiguredAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyProduct" ALTER COLUMN "syncedAt" TYPE TIMESTAMPTZ(3) USING "syncedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyProduct" ALTER COLUMN "lastDiscoveryAt" TYPE TIMESTAMPTZ(3) USING "lastDiscoveryAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyProduct" ALTER COLUMN "lastDecisionAt" TYPE TIMESTAMPTZ(3) USING "lastDecisionAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyProduct" ALTER COLUMN "semanticClaimedAt" TYPE TIMESTAMPTZ(3) USING "semanticClaimedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyUser" ALTER COLUMN "installedAt" TYPE TIMESTAMPTZ(3) USING "installedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyUser" ALTER COLUMN "productSyncStartedAt" TYPE TIMESTAMPTZ(3) USING "productSyncStartedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyUser" ALTER COLUMN "productSyncedAt" TYPE TIMESTAMPTZ(3) USING "productSyncedAt" AT TIME ZONE 'UTC';
ALTER TABLE "ShopifyVariant" ALTER COLUMN "updatedAt" TYPE TIMESTAMPTZ(3) USING "updatedAt" AT TIME ZONE 'UTC';
ALTER TABLE "VariantCompetitorStats" ALTER COLUMN "lastUpdatedAt" TYPE TIMESTAMPTZ(3) USING "lastUpdatedAt" AT TIME ZONE 'UTC';
