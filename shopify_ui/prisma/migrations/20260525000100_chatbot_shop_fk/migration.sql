ALTER TABLE "ChatSession" ADD CONSTRAINT "ChatSession_shopDomain_fkey" FOREIGN KEY ("shopDomain") REFERENCES "ShopifyUser"("shopDomain") ON DELETE RESTRICT ON UPDATE CASCADE;
