-- AlterTable
ALTER TABLE "CompetitorCandidate" ADD COLUMN     "embedScore" DECIMAL(5,4);

-- AlterTable
ALTER TABLE "ShopifyProduct" ADD COLUMN     "searchQuery" TEXT;
