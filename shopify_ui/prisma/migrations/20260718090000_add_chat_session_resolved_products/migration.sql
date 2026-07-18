-- AlterTable
ALTER TABLE "ChatSession" ADD COLUMN "resolvedProductIds" JSONB NOT NULL DEFAULT '[]';
