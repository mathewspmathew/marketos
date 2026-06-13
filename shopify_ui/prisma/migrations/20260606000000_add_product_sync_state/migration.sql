ALTER TABLE "ShopifyUser"
  ADD COLUMN "productSyncState"     TEXT         NOT NULL DEFAULT 'IDLE',
  ADD COLUMN "productSyncStartedAt" TIMESTAMPTZ,
  ADD COLUMN "productSyncedAt"      TIMESTAMPTZ,
  ADD COLUMN "productSyncError"     TEXT;
