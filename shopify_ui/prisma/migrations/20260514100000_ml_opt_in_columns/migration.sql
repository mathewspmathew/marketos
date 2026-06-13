-- Shadow ML + per-variant opt-in (Option A).
-- Pricing engine records what ML would have predicted on every decision,
-- but rules win unless the merchant explicitly opts in.

ALTER TABLE "PriceDecision"
  ADD COLUMN "ruleSuggestedPrice" DECIMAL(10,2),
  ADD COLUMN "mlSuggestedPrice"   DECIMAL(10,2),
  ADD COLUMN "mlConfidence"       DECIMAL(4,3),
  ADD COLUMN "modelVersion"       TEXT;

-- Per-rule blending dial. 0.0 = pure rule, 1.0 = pure ML.
-- Default 0.0 keeps existing rules behaving exactly as today.
ALTER TABLE "PricingRule"
  ADD COLUMN "mlBlendWeight" DECIMAL(3,2) NOT NULL DEFAULT 0.0;

-- Per-variant ML opt-in. Overrides the rule's blend weight when true,
-- giving merchants fine-grained control to trust ML on specific SKUs.
ALTER TABLE "ShopifyVariant"
  ADD COLUMN "useMlSuggestion" BOOLEAN NOT NULL DEFAULT false;
