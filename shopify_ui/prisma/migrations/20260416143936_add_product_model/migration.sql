-- CreateTable
CREATE TABLE "Product" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "shop" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "description" TEXT NOT NULL DEFAULT '',
    "price" TEXT NOT NULL DEFAULT '0.00',
    "compareAtPrice" TEXT,
    "tags" TEXT NOT NULL DEFAULT '[]',
    "productType" TEXT NOT NULL DEFAULT '',
    "imageUrl" TEXT,
    "status" TEXT NOT NULL DEFAULT 'ACTIVE',
    "dynamicChangeEnabled" BOOLEAN NOT NULL DEFAULT false,
    "fieldPrice" BOOLEAN NOT NULL DEFAULT true,
    "fieldDescription" BOOLEAN NOT NULL DEFAULT false,
    "fieldTitle" BOOLEAN NOT NULL DEFAULT false,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" DATETIME NOT NULL
);
