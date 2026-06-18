-- Drop unused sync fields from ShopifyProduct
ALTER TABLE "ShopifyProduct" DROP COLUMN "syncDescription";
ALTER TABLE "ShopifyProduct" DROP COLUMN "syncTitle";
