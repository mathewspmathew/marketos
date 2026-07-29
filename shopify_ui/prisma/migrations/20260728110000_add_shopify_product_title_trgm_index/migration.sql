-- resolve_product (chatbot_svc) runs word_similarity(lower(:ref), lower(title))
-- over every ShopifyProduct row on every fuzzy product lookup, with no index
-- to back it -- a full sequential scan per call. pg_trgm is already enabled
-- (see 20260615051510_make_discovery_num_results_nullable_and_add_to_shop_settings).
CREATE INDEX "ShopifyProduct_title_trgm_idx" ON "ShopifyProduct" USING gin (lower("title") gin_trgm_ops);
