-- Track when each embedding was last consumed by the matcher.
-- Enables event-driven matching: dirty embeddings (matchedAt < updatedAt/vectorizedAt
-- or NULL) are picked up by match_for_shop(full=False) instead of needing a
-- nightly full sweep.

ALTER TABLE "ShopifyEmbedding" ADD COLUMN "matchedAt" TIMESTAMP(3);
ALTER TABLE "ProductEmbedding" ADD COLUMN "matchedAt" TIMESTAMP(3);
