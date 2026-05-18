-- Structured fingerprint columns parsed from the new semanticText output.
-- Used by the matcher SQL pre-filter as a hard gate before HNSW similarity.

ALTER TABLE "ShopifyProduct"
  ADD COLUMN IF NOT EXISTS "categoryTop"   TEXT,
  ADD COLUMN IF NOT EXISTS "productGender" TEXT;

ALTER TABLE "ScrapedProduct"
  ADD COLUMN IF NOT EXISTS "categoryTop"   TEXT,
  ADD COLUMN IF NOT EXISTS "productGender" TEXT;
