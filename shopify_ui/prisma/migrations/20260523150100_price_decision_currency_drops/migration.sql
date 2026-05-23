-- Count competitor observations dropped due to currency mismatch so the
-- stats page can explain why below_min_competitors fired.
ALTER TABLE "PriceDecision"
    ADD COLUMN "currencyDrops" INTEGER NOT NULL DEFAULT 0;
