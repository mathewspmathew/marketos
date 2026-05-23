-- Auto-pricing v2: tier, basePrice anchor, per-round + lifetime caps.

CREATE TYPE "PricingTier" AS ENUM ('BUDGET', 'COMPETITIVE', 'PREMIUM');

ALTER TABLE "ShopifyProduct"
    ADD COLUMN "pricingTier"                   "PricingTier" NOT NULL DEFAULT 'COMPETITIVE',
    ADD COLUMN "basePrice"                     DECIMAL(10,2),
    ADD COLUMN "minPriceOverride"              DECIMAL(10,2),
    ADD COLUMN "maxPriceOverride"              DECIMAL(10,2),
    ADD COLUMN "maxAutoApplyChangePctOverride" DOUBLE PRECISION,
    ADD COLUMN "lifetimeCapPctOverride"        DOUBLE PRECISION,
    ADD COLUMN "lastDecisionAt"                TIMESTAMP(3);

ALTER TABLE "ShopifyVariant"
    ADD COLUMN "basePrice" DECIMAL(10,2);

ALTER TABLE "ShopSettings"
    ADD COLUMN "minCompetitorsToPrice" INTEGER          NOT NULL DEFAULT 4,
    ADD COLUMN "topKCompetitors"       INTEGER          NOT NULL DEFAULT 4,
    ADD COLUMN "maxAutoApplyChangePct" DOUBLE PRECISION NOT NULL DEFAULT 0.05,
    ADD COLUMN "lifetimeCapPct"        DOUBLE PRECISION NOT NULL DEFAULT 0.25,
    ADD COLUMN "budgetUndercut"        DOUBLE PRECISION NOT NULL DEFAULT 0.05,
    ADD COLUMN "premiumUplift"         DOUBLE PRECISION NOT NULL DEFAULT 0.05;

ALTER TABLE "PriceDecision"
    ADD COLUMN "changePct"       DOUBLE PRECISION,
    ADD COLUMN "refPrice"        DECIMAL(10,2),
    ADD COLUMN "formulaTarget"   DECIMAL(10,2),
    ADD COLUMN "competitorsUsed" INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN "topMatchesJson"  JSONB,
    ADD COLUMN "tierAtDecision"  "PricingTier",
    ADD COLUMN "skipReason"      TEXT,
    ADD COLUMN "autoApplied"     BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN "applyError"      TEXT;
