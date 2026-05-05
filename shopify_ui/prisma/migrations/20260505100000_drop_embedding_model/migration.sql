-- AlterTable: drop embeddingModel from ProductEmbedding (removed from Prisma schema)
ALTER TABLE "ProductEmbedding" DROP COLUMN IF EXISTS "embeddingModel";
