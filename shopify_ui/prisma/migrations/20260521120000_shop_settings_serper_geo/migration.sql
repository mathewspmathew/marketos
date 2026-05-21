-- Per-shop Serper geo/language targeting for competitor discovery.
ALTER TABLE "ShopSettings"
  ADD COLUMN "serperGl"       TEXT NOT NULL DEFAULT 'in',
  ADD COLUMN "serperHl"       TEXT NOT NULL DEFAULT 'en',
  ADD COLUMN "serperLocation" TEXT NOT NULL DEFAULT 'Kochi, Kerala';
