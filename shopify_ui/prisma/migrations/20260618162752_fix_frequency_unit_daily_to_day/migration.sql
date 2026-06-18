/*
  Warnings:

  - You are about to drop the `ActivityEvent` table. If the table is not empty, all the data it contains will be lost.

*/
-- DropForeignKey
ALTER TABLE "ActivityEvent" DROP CONSTRAINT "ActivityEvent_competitorProductId_fkey";

-- DropForeignKey
ALTER TABLE "ActivityEvent" DROP CONSTRAINT "ActivityEvent_competitorVariantId_fkey";

-- DropForeignKey
ALTER TABLE "ActivityEvent" DROP CONSTRAINT "ActivityEvent_priceDecisionId_fkey";

-- DropForeignKey
ALTER TABLE "ActivityEvent" DROP CONSTRAINT "ActivityEvent_shopDomain_fkey";

-- DropForeignKey
ALTER TABLE "ActivityEvent" DROP CONSTRAINT "ActivityEvent_shopifyProductId_fkey";

-- DropForeignKey
ALTER TABLE "ActivityEvent" DROP CONSTRAINT "ActivityEvent_shopifyVariantId_fkey";

-- AlterTable
ALTER TABLE "ProductUrl" ALTER COLUMN "frequencyUnit" SET DEFAULT 'day';

-- AlterTable
ALTER TABLE "ShopSettings" ALTER COLUMN "frequencyUnit" SET DEFAULT 'day';

-- DropTable
DROP TABLE "ActivityEvent";

-- DropEnum
DROP TYPE "ActivityEventType";
