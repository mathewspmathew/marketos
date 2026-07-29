-- Merchant master switch: pause pushing calculated prices to Shopify without
-- disabling dynamic pricing or losing calculation history. Default TRUE
-- preserves today's always-push behavior for existing shops.

ALTER TABLE "ShopSettings"
    ADD COLUMN "autoUpdatePriceEnabled" BOOLEAN NOT NULL DEFAULT TRUE;
