-- Drop denormalized fields from DiscoveryJob
ALTER TABLE "DiscoveryJob" DROP COLUMN "candidateCount";
ALTER TABLE "DiscoveryJob" DROP COLUMN "numResults";
ALTER TABLE "DiscoveryJob" DROP COLUMN "listingExpansionCap";
