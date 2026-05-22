-- Currency-aware matching:
--   * ShopSettings.currency — ISO-4217 code for the merchant store (Shopify shop.currencyCode).
--   * ProductMatch.currencyMismatch — set by the matcher when shop and competitor currencies differ.

ALTER TABLE "ShopSettings"
  ADD COLUMN "currency" TEXT;

ALTER TABLE "ProductMatch"
  ADD COLUMN "currencyMismatch" BOOLEAN NOT NULL DEFAULT FALSE;
