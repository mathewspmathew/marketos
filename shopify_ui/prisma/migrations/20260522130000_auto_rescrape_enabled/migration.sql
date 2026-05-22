-- Global pause for all rescrape work in this shop. Distinct from killSwitch
-- (which gates PriceDecision writes).
ALTER TABLE "ShopSettings"
ADD COLUMN "autoRescrapeEnabled" BOOLEAN NOT NULL DEFAULT TRUE;
