-- DiscoveryJob now carries the user-supplied query + how many results to keep.
ALTER TABLE "DiscoveryJob"
  ADD COLUMN "query" TEXT,
  ADD COLUMN "numResults" INTEGER NOT NULL DEFAULT 10;
