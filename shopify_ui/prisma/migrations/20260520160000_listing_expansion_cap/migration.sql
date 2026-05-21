-- Listing-page expansion cap with 3-level fallback:
-- DiscoveryJob.listingExpansionCap → ShopifyProduct.listingExpansionCap
-- → ShopSettings.listingExpansionCap → hard default (5) in app code.
-- All nullable so existing rows keep working without backfill.

ALTER TABLE "ShopifyProduct" ADD COLUMN "listingExpansionCap" INTEGER;
ALTER TABLE "DiscoveryJob"   ADD COLUMN "listingExpansionCap" INTEGER;
ALTER TABLE "ShopSettings"   ADD COLUMN "listingExpansionCap" INTEGER;
