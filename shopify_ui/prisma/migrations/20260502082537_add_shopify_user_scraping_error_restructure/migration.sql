/*
  Warnings:

  - You are about to drop the column `userId` on the `ProductEmbedding` table. All the data in the column will be lost.
  - You are about to drop the column `userId` on the `ProductUrl` table. All the data in the column will be lost.
  - You are about to drop the column `userId` on the `ScrapedProduct` table. All the data in the column will be lost.
  - You are about to drop the column `userId` on the `ScrapedVariant` table. All the data in the column will be lost.
  - You are about to drop the column `shopId` on the `ScrapingConfig` table. All the data in the column will be lost.
  - You are about to drop the column `userId` on the `ScrapingConfig` table. All the data in the column will be lost.
  - You are about to drop the column `dynamicChangeEnabled` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `fieldDescription` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `fieldPrice` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `fieldTitle` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `imageEmbedding` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `semanticText` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `shop` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `specifications` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `textEmbedding` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `url` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `userId` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `vectorized` on the `ShopifyProduct` table. All the data in the column will be lost.
  - You are about to drop the column `originalPrice` on the `ShopifyVariant` table. All the data in the column will be lost.
  - You are about to drop the column `userId` on the `ShopifyVariant` table. All the data in the column will be lost.
  - You are about to drop the column `variantEmbedding` on the `ShopifyVariant` table. All the data in the column will be lost.
  - You are about to drop the column `variantSemanticText` on the `ShopifyVariant` table. All the data in the column will be lost.
  - You are about to drop the column `vectorized` on the `ShopifyVariant` table. All the data in the column will be lost.
  - You are about to drop the `User` table. If the table is not empty, all the data it contains will be lost.
  - Added the required column `shopDomain` to the `ProductEmbedding` table without a default value. This is not possible if the table is not empty.
  - Added the required column `shopDomain` to the `ProductUrl` table without a default value. This is not possible if the table is not empty.
  - Added the required column `shopDomain` to the `ScrapedProduct` table without a default value. This is not possible if the table is not empty.
  - Added the required column `shopDomain` to the `ScrapingConfig` table without a default value. This is not possible if the table is not empty.
  - Added the required column `shopDomain` to the `ShopifyProduct` table without a default value. This is not possible if the table is not empty.

*/
-- DropForeignKey
ALTER TABLE "ProductEmbedding" DROP CONSTRAINT "ProductEmbedding_userId_fkey";

-- DropForeignKey
ALTER TABLE "ProductUrl" DROP CONSTRAINT "ProductUrl_userId_fkey";

-- DropForeignKey
ALTER TABLE "ScrapedProduct" DROP CONSTRAINT "ScrapedProduct_userId_fkey";

-- DropForeignKey
ALTER TABLE "ScrapedVariant" DROP CONSTRAINT "ScrapedVariant_userId_fkey";

-- DropForeignKey
ALTER TABLE "ScrapingConfig" DROP CONSTRAINT "ScrapingConfig_userId_fkey";

-- DropForeignKey
ALTER TABLE "ShopifyProduct" DROP CONSTRAINT "ShopifyProduct_userId_fkey";

-- DropForeignKey
ALTER TABLE "ShopifyVariant" DROP CONSTRAINT "ShopifyVariant_userId_fkey";

-- AlterTable
ALTER TABLE "ProductEmbedding" DROP COLUMN "userId",
ADD COLUMN     "shopDomain" TEXT NOT NULL;

-- AlterTable
ALTER TABLE "ProductUrl" DROP COLUMN "userId",
ADD COLUMN     "shopDomain" TEXT NOT NULL;

-- AlterTable
ALTER TABLE "ScrapedProduct" DROP COLUMN "userId",
ADD COLUMN     "shopDomain" TEXT NOT NULL;

-- AlterTable
ALTER TABLE "ScrapedVariant" DROP COLUMN "userId";

-- AlterTable
ALTER TABLE "ScrapingConfig" DROP COLUMN "shopId",
DROP COLUMN "userId",
ADD COLUMN     "shopDomain" TEXT NOT NULL;

-- AlterTable
ALTER TABLE "ShopifyProduct" DROP COLUMN "dynamicChangeEnabled",
DROP COLUMN "fieldDescription",
DROP COLUMN "fieldPrice",
DROP COLUMN "fieldTitle",
DROP COLUMN "imageEmbedding",
DROP COLUMN "semanticText",
DROP COLUMN "shop",
DROP COLUMN "specifications",
DROP COLUMN "textEmbedding",
DROP COLUMN "url",
DROP COLUMN "userId",
DROP COLUMN "vectorized",
ADD COLUMN     "dynamicPricingEnabled" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "handle" TEXT,
ADD COLUMN     "shopDomain" TEXT NOT NULL,
ADD COLUMN     "syncDescription" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "syncPrice" BOOLEAN NOT NULL DEFAULT true,
ADD COLUMN     "syncTitle" BOOLEAN NOT NULL DEFAULT false,
ADD COLUMN     "syncedAt" TIMESTAMP(3);

-- AlterTable
ALTER TABLE "ShopifyVariant" DROP COLUMN "originalPrice",
DROP COLUMN "userId",
DROP COLUMN "variantEmbedding",
DROP COLUMN "variantSemanticText",
DROP COLUMN "vectorized",
ADD COLUMN     "compareAtPrice" DECIMAL(10,2),
ADD COLUMN     "imageUrl" TEXT;

-- DropTable
DROP TABLE "User";

-- CreateTable
CREATE TABLE "ShopifyUser" (
    "shopDomain" TEXT NOT NULL,
    "shopifyUserId" BIGINT,
    "email" TEXT,
    "firstName" TEXT,
    "installedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ShopifyUser_pkey" PRIMARY KEY ("shopDomain")
);

-- CreateTable
CREATE TABLE "ShopifyVectorized" (
    "id" TEXT NOT NULL,
    "variantId" TEXT NOT NULL,
    "semanticText" TEXT,
    "textEmbedding" vector(768),
    "imageEmbedding" vector(768),
    "embeddedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ShopifyVectorized_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ScrapingError" (
    "id" TEXT NOT NULL,
    "shopDomain" TEXT NOT NULL,
    "configId" TEXT NOT NULL,
    "productUrl" TEXT NOT NULL,
    "gcsRef" TEXT,
    "errorType" TEXT NOT NULL,
    "errorDetail" TEXT,
    "taskName" TEXT NOT NULL,
    "failedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "ScrapingError_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "ShopifyVectorized_variantId_key" ON "ShopifyVectorized"("variantId");

-- AddForeignKey
ALTER TABLE "ShopifyProduct" ADD CONSTRAINT "ShopifyProduct_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ShopifyVectorized" ADD CONSTRAINT "ShopifyVectorized_variantId_fkey" FOREIGN KEY ("variantId") REFERENCES "ShopifyVariant"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ScrapedProduct" ADD CONSTRAINT "ScrapedProduct_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductUrl" ADD CONSTRAINT "ProductUrl_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ProductEmbedding" ADD CONSTRAINT "ProductEmbedding_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ScrapingConfig" ADD CONSTRAINT "ScrapingConfig_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ScrapingError" ADD CONSTRAINT "ScrapingError_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ScrapingError" ADD CONSTRAINT "ScrapingError_configId_fkey" FOREIGN KEY ("configId") REFERENCES "ScrapingConfig"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
