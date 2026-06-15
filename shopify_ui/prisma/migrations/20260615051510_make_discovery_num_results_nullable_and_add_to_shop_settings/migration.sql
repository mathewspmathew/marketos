/*
  Warnings:

  - You are about to drop the column `killSwitch` on the `ShopSettings` table. All the data in the column will be lost.
  - You are about to drop the column `minCompetitorsRequired` on the `ShopSettings` table. All the data in the column will be lost.

*/
-- CreateExtension
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- AlterTable
ALTER TABLE "ShopSettings" DROP COLUMN "killSwitch",
DROP COLUMN "minCompetitorsRequired",
ADD COLUMN     "discoveryNumResults" INTEGER NOT NULL DEFAULT 10;

-- AlterTable
ALTER TABLE "ShopifyProduct" ALTER COLUMN "discoveryNumResults" DROP NOT NULL,
ALTER COLUMN "discoveryNumResults" DROP DEFAULT,
ALTER COLUMN "semanticClaimedAt" SET DATA TYPE TIMESTAMP(3);

-- AlterTable
ALTER TABLE "ShopifyUser" ALTER COLUMN "productSyncStartedAt" SET DATA TYPE TIMESTAMP(3),
ALTER COLUMN "productSyncedAt" SET DATA TYPE TIMESTAMP(3);
