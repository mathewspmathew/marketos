-- Retire VariantPriceSuggestion. Pricing is now owned by PriceDecision +
-- PriceChange (rule-driven via pricing_svc + shopify_writer). The old
-- suggestion table is unused.

DROP TABLE IF EXISTS "VariantPriceSuggestion";
