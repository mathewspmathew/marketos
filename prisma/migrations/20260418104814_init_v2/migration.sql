/*
  Warnings:

  - You are about to drop the `Product` table. If the table is not empty, all the data it contains will be lost.

*/
-- DropTable
DROP TABLE "Product";

-- DropEnum
DROP TYPE "ProductSource";

-- CreateTable
CREATE TABLE "User" (
    "id" TEXT NOT NULL,
    "username" TEXT,
    "email" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "User_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ShopifyProduct" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "shop" TEXT NOT NULL,
    "url" TEXT,
    "title" TEXT NOT NULL,
    "description" TEXT NOT NULL DEFAULT '',
    "vendor" TEXT,
    "productType" TEXT NOT NULL DEFAULT '',
    "tags" JSONB NOT NULL DEFAULT '[]',
    "imageUrl" TEXT,
    "specifications" JSONB,
    "semanticText" TEXT,
    "textEmbedding" vector(768),
    "imageEmbedding" vector(512),
    "vectorized" BOOLEAN NOT NULL DEFAULT false,
    "dynamicChangeEnabled" BOOLEAN NOT NULL DEFAULT false,
    "fieldPrice" BOOLEAN NOT NULL DEFAULT true,
    "fieldDescription" BOOLEAN NOT NULL DEFAULT false,
    "fieldTitle" BOOLEAN NOT NULL DEFAULT false,
    "status" TEXT NOT NULL DEFAULT 'ACTIVE',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ShopifyProduct_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ShopifyVariant" (
    "id" TEXT NOT NULL,
    "productId" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "sku" TEXT,
    "barcode" TEXT,
    "title" TEXT NOT NULL DEFAULT 'Default Title',
    "options" JSONB,
    "currentPrice" DECIMAL(10,2) NOT NULL,
    "originalPrice" DECIMAL(10,2),
    "isInStock" BOOLEAN NOT NULL DEFAULT true,
    "stockQuantity" INTEGER,
    "variantSemanticText" TEXT,
    "variantEmbedding" vector(768),
    "vectorized" BOOLEAN NOT NULL DEFAULT false,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ShopifyVariant_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ScrapedProduct" (
    "id" TEXT NOT NULL,
    "url" TEXT NOT NULL,
    "domain" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "description" TEXT,
    "vendor" TEXT,
    "productType" TEXT,
    "tags" JSONB NOT NULL DEFAULT '[]',
    "imageUrl" TEXT,
    "specifications" JSONB,
    "semanticText" TEXT,
    "textEmbedding" vector(768),
    "imageEmbedding" vector(512),
    "vectorized" BOOLEAN NOT NULL DEFAULT false,
    "scrapedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ScrapedProduct_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "ScrapedVariant" (
    "id" TEXT NOT NULL,
    "productId" TEXT NOT NULL,
    "sku" TEXT,
    "barcode" TEXT,
    "title" TEXT NOT NULL DEFAULT 'Default Title',
    "options" JSONB,
    "currentPrice" DECIMAL(10,2) NOT NULL,
    "originalPrice" DECIMAL(10,2),
    "isInStock" BOOLEAN NOT NULL DEFAULT true,
    "stockQuantity" INTEGER,
    "variantSemanticText" TEXT,
    "variantEmbedding" vector(768),
    "vectorized" BOOLEAN NOT NULL DEFAULT false,
    "scrapedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ScrapedVariant_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE UNIQUE INDEX "User_email_key" ON "User"("email");

-- CreateIndex
CREATE UNIQUE INDEX "ScrapedProduct_url_key" ON "ScrapedProduct"("url");

-- AddForeignKey
ALTER TABLE "ShopifyProduct" ADD CONSTRAINT "ShopifyProduct_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ShopifyVariant" ADD CONSTRAINT "ShopifyVariant_productId_fkey" FOREIGN KEY ("productId") REFERENCES "ShopifyProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ShopifyVariant" ADD CONSTRAINT "ShopifyVariant_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ScrapedVariant" ADD CONSTRAINT "ScrapedVariant_productId_fkey" FOREIGN KEY ("productId") REFERENCES "ScrapedProduct"("id") ON DELETE CASCADE ON UPDATE CASCADE;
