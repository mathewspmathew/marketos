-- ShopifyVariant had no index on its ShopifyProduct foreign key, forcing a
-- sequential scan for every per-product variant lookup (app.products.jsx's
-- findMany include and the semanticText count query). Postgres doesn't
-- auto-index FK source columns, so this needs to be explicit.
CREATE INDEX "ShopifyVariant_productId_idx" ON "ShopifyVariant"("productId");
