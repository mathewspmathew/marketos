/*
  Warnings:

  - You are about to drop the column `competitorTrackingEnabled` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `autoPriceEnabled` on the `ShopifyVariant` table. All the data in the column will be lost.
  - You are about to drop the column `ceilingPrice` on the `ShopifyVariant` table. All the data in the column will be lost.
  - You are about to drop the column `floorPrice` on the `ShopifyVariant` table. All the data in the column will be lost.

*/
-- AlterTable
ALTER TABLE "ShopSettings" ADD COLUMN     "frequencyInterval" INTEGER NOT NULL DEFAULT 1,
ADD COLUMN     "frequencyUnit" TEXT NOT NULL DEFAULT 'daily',
ADD COLUMN     "maxCompetitorsPerProduct" INTEGER NOT NULL DEFAULT 8;

-- AlterTable
ALTER TABLE "ShopifyProduct" DROP COLUMN "competitorTrackingEnabled",
ADD COLUMN     "ceilingPrice" DECIMAL(10,2),
ADD COLUMN     "floorPrice" DECIMAL(10,2),
ADD COLUMN     "frequencyInterval" INTEGER,
ADD COLUMN     "frequencyUnit" TEXT,
ADD COLUMN     "searchQueryOverride" TEXT;

-- AlterTable
ALTER TABLE "ShopifyVariant" DROP COLUMN "autoPriceEnabled",
DROP COLUMN "ceilingPrice",
DROP COLUMN "floorPrice";
