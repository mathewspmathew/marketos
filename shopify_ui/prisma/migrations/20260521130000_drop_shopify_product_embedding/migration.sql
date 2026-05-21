-- DropForeignKey
ALTER TABLE "ShopifyProductEmbedding" DROP CONSTRAINT IF EXISTS "ShopifyProductEmbedding_productId_fkey";

-- DropTable
DROP TABLE IF EXISTS "ShopifyProductEmbedding";
