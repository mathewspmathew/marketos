-- Track how many competitor observations were out-of-stock when this decision ran.
ALTER TABLE "PriceDecision"
    ADD COLUMN "oosObservations" INTEGER NOT NULL DEFAULT 0;
