-- DropIndex
DROP INDEX "ProductUrl_url_key";

-- CreateIndex
CREATE UNIQUE INDEX "ProductUrl_shopifyProductId_url_key" ON "ProductUrl"("shopifyProductId", "url");
