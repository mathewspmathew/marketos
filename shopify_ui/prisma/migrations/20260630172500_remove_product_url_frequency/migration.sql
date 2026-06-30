-- DropIndex
DROP INDEX "ProductUrl_shopDomain_status_nextRunAt_idx";

-- AlterTable
ALTER TABLE "ProductUrl" DROP COLUMN "frequencyInterval",
DROP COLUMN "frequencyUnit";

-- CreateIndex
CREATE INDEX "ProductUrl_shopDomain_status_nextRunAt_idx" ON "ProductUrl"("shopDomain", "status", "nextRunAt");
