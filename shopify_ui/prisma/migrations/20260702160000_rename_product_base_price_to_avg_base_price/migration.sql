-- Product-level basePrice was a min-of-variants snapshot; it is now the
-- average of variant basePrices (display/reporting only — the pricing
-- engine anchors on variant basePrice).
ALTER TABLE "ShopifyProduct" RENAME COLUMN "basePrice" TO "avgBasePrice";
