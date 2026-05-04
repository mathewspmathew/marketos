/*
  Warnings:

  - You are about to drop the `ShopifyVectorized` table. If the table is not empty, all the data it contains will be lost.

*/
-- DropForeignKey
ALTER TABLE "ShopifyVectorized" DROP CONSTRAINT "ShopifyVectorized_variantId_fkey";

-- DropTable
DROP TABLE "ShopifyVectorized";

-- CreateTable
CREATE TABLE "ShopifyEmbedding" (
    "id" TEXT NOT NULL,
    "variantId" TEXT NOT NULL,
    "semanticText" TEXT,
    "textEmbedding" vector(768),
    "imageEmbedding" vector(768),
    "embeddedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ShopifyEmbedding_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "ShopifyEmbedding_variantId_key" ON "ShopifyEmbedding"("variantId");

-- AddForeignKey
ALTER TABLE "ShopifyEmbedding" ADD CONSTRAINT "ShopifyEmbedding_variantId_fkey" FOREIGN KEY ("variantId") REFERENCES "ShopifyVariant"("id") ON DELETE CASCADE ON UPDATE CASCADE;
