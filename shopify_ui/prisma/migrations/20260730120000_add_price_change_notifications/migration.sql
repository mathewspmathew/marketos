-- Per-shop price-change email notifications: one address, off by default
-- for fresh installs until the merchant explicitly enables it in Settings.
ALTER TABLE "ShopSettings"
    ADD COLUMN "notifyEmail" TEXT,
    ADD COLUMN "priceChangeNotificationsEnabled" BOOLEAN NOT NULL DEFAULT FALSE;
