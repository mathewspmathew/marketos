/*
  Warnings:

  - You are about to drop the column `imageEmbedding` on the `ScrapedProduct` table. All the data in the column will be lost.
  - You are about to drop the column `scrapedAt` on the `ScrapedProduct` table. All the data in the column will be lost.
  - You are about to drop the column `semanticText` on the `ScrapedProduct` table. All the data in the column will be lost.
  - You are about to drop the column `textEmbedding` on the `ScrapedProduct` table. All the data in the column will be lost.
  - You are about to drop the column `url` on the `ScrapedProduct` table. All the data in the column will be lost.
  - You are about to drop the column `vectorized` on the `ScrapedProduct` table. All the data in the column will be lost.
  - You are about to drop the column `scrapedAt` on the `ScrapedVariant` table. All the data in the column will be lost.
  - You are about to drop the column `variantEmbedding` on the `ScrapedVariant` table. All the data in the column will be lost.
  - You are about to drop the column `variantSemanticText` on the `ScrapedVariant` table. All the data in the column will be lost.
  - You are about to drop the column `vectorized` on the `ScrapedVariant` table. All the data in the column will be lost.
  - You are about to drop the column `llmInstructions` on the `ScrapingConfig` table. All the data in the column will be lost.

*/
-- CreateEnum
CREATE TYPE "UrlStatus" AS ENUM ('ACTIVE', 'DEAD', 'PAUSED');

-- CreateEnum
CREATE TYPE "ScrapeStatus" AS ENUM ('IDLE', 'QUEUED', 'RUNNING', 'SCRAPED_FIRST');

-- DropIndex
DROP INDEX "ScrapedProduct_url_key";

-- AlterTable
ALTER TABLE "ScrapedProduct" DROP COLUMN "imageEmbedding",
DROP COLUMN "scrapedAt",
DROP COLUMN "semanticText",
DROP COLUMN "textEmbedding",
DROP COLUMN "url",
DROP COLUMN "vectorized",
ADD COLUMN     "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP;

-- AlterTable
ALTER TABLE "ScrapedVariant" DROP COLUMN "scrapedAt",
DROP COLUMN "variantEmbedding",
DROP COLUMN "variantSemanticText",
DROP COLUMN "vectorized",
ADD COLUMN     "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
ADD COLUMN     "semanticText" TEXT;

-- AlterTable
ALTER TABLE "ScrapingConfig" DROP COLUMN "llmInstructions",
ADD COLUMN     "status" "ScrapeStatus" NOT NULL DEFAULT 'IDLE',
ALTER COLUMN "shopId" DROP NOT NULL,
ALTER COLUMN "frequencyInterval" DROP NOT NULL;

-- CreateTable
CREATE TABLE "ProductUrl" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "configId" TEXT NOT NULL,
    "prodId" TEXT NOT NULL,
    "url" TEXT NOT NULL,
    "status" "UrlStatus" NOT NULL DEFAULT 'ACTIVE',
    "failCount" INTEGER NOT NULL DEFAULT 0,
    "lastScrapedAt" TIMESTAMP(3),
    "nextScrapAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ProductUrl_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ProductEmbedding" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "prodId" TEXT NOT NULL,
    "variantId" TEXT,
    "vectorText" vector(768),
    "vectorImg" vector(768),
    "embeddingModel" TEXT NOT NULL,
    "vectorizedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ProductEmbedding_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "ProductUrl_url_key" ON "ProductUrl"("url");

-- AddForeignKey
ALTER TABLE "ProductUrl" ADD CONSTRAINT "ProductUrl_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductUrl" ADD CONSTRAINT "ProductUrl_configId_fkey" FOREIGN KEY ("configId") REFERENCES "ScrapingConfig"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductUrl" ADD CONSTRAINT "ProductUrl_prodId_fkey" FOREIGN KEY ("prodId") REFERENCES "ScrapedProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductEmbedding" ADD CONSTRAINT "ProductEmbedding_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductEmbedding" ADD CONSTRAINT "ProductEmbedding_prodId_fkey" FOREIGN KEY ("prodId") REFERENCES "ScrapedProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductEmbedding" ADD CONSTRAINT "ProductEmbedding_variantId_fkey" FOREIGN KEY ("variantId") REFERENCES "ScrapedVariant"("id") ON DELETE SET NULL ON UPDATE CASCADE;
