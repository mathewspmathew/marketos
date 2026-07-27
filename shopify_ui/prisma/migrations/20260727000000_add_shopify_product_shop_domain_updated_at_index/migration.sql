-- ShopifyProduct was the only shop-scoped model with no index beyond its
-- primary key. Matches the orderBy(updatedAt desc) used when listing a
-- shop's products.
CREATE INDEX "ShopifyProduct_shopDomain_updatedAt_idx" ON "ShopifyProduct"("shopDomain", "updatedAt");
