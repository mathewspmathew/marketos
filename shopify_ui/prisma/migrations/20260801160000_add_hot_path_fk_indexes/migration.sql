-- Missing FK indexes confirmed by grepping real (non-test) call sites for
-- direct WHERE/JOIN usage, plus onDelete: Cascade columns whose parent gets
-- deleted by app code (services/common/pane_config.py's delete_dynamic_pricing
-- and friends) — without an index, a cascade delete forces Postgres to
-- sequentially scan the child table while holding a lock on it.
CREATE INDEX "ChatPreview_sessionId_idx" ON "ChatPreview"("sessionId");
CREATE INDEX "CompetitorCandidate_scrapedProductId_idx" ON "CompetitorCandidate"("scrapedProductId");
CREATE INDEX "ProductEmbedding_prodId_idx" ON "ProductEmbedding"("prodId");
CREATE INDEX "ProductLevelMatch_scrapedProductId_idx" ON "ProductLevelMatch"("scrapedProductId");
CREATE INDEX "ProductMatch_competitorProdId_idx" ON "ProductMatch"("competitorProdId");
CREATE INDEX "ProductMatch_productMatchId_idx" ON "ProductMatch"("productMatchId");
CREATE INDEX "ProductUrl_prodId_idx" ON "ProductUrl"("prodId");
CREATE INDEX "ScrapedVariant_productId_idx" ON "ScrapedVariant"("productId");
