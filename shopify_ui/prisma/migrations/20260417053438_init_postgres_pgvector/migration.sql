-- CreateExtension
CREATE EXTENSION IF NOT EXISTS "vector";

-- CreateEnum
CREATE TYPE "ProductSource" AS ENUM ('INTERNAL', 'COMPETITOR');

-- CreateTable
CREATE TABLE "Session" (
    "id" TEXT NOT NULL,
    "shop" TEXT NOT NULL,
    "state" TEXT NOT NULL,
    "isOnline" BOOLEAN NOT NULL DEFAULT false,
    "scope" TEXT,
    "expires" TIMESTAMP(3),
    "accessToken" TEXT NOT NULL,
    "userId" BIGINT,
    "firstName" TEXT,
    "lastName" TEXT,
    "email" TEXT,
    "accountOwner" BOOLEAN NOT NULL DEFAULT false,
    "locale" TEXT,
    "collaborator" BOOLEAN DEFAULT false,
    "emailVerified" BOOLEAN DEFAULT false,
    "refreshToken" TEXT,
    "refreshTokenExpires" TIMESTAMP(3),

    CONSTRAINT "Session_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "Product" (
    "id" TEXT NOT NULL,
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
    "source" "ProductSource" NOT NULL DEFAULT 'INTERNAL',
    "vectorized" BOOLEAN NOT NULL DEFAULT false,
    "textEmbedding" vector(768),
    "imageEmbedding" vector(512),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "Product_pkey" PRIMARY KEY ("id")
);
