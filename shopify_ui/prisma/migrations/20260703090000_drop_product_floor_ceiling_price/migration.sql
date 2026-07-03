-- floorPrice/ceilingPrice on ShopifyProduct were a legacy bounds pair that
-- competed with minPriceOverride/maxPriceOverride (both NULL for every row).
-- minPriceOverride/maxPriceOverride are now the single source of bounds.
-- (PricingRule keeps its own floorPrice/ceilingPrice — different feature.)
ALTER TABLE "ShopifyProduct" DROP COLUMN "floorPrice";
ALTER TABLE "ShopifyProduct" DROP COLUMN "ceilingPrice";
