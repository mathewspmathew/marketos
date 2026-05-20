/*
  Warnings:

  - You are about to drop the column `searchQueryVector` on the `ShopifyProduct` table. All the data in the column will be lost.

*/
-- AlterTable
ALTER TABLE "ShopifyProduct" DROP COLUMN "searchQueryVector";

-- CreateTable
CREATE TABLE "ShopifyProductEmbedding" (
    "productId" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "searchQueryVector" vector(768),
    "embeddedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ShopifyProductEmbedding_pkey" PRIMARY KEY ("productId")
);

-- CreateIndex
CREATE INDEX "ShopifyProductEmbedding_shopDomain_idx" ON "ShopifyProductEmbedding"("shopDomain");

-- AddForeignKey
ALTER TABLE "ShopifyProductEmbedding" ADD CONSTRAINT "ShopifyProductEmbedding_productId_fkey" FOREIGN KEY ("productId") REFERENCES "ShopifyProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;
