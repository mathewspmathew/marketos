-- Image embedding columns widened from vector(768) to vector(1408)
-- (multimodalembedding@001 supports 128/256/512/1408 only).
ALTER TABLE "ProductEmbedding" ALTER COLUMN "vectorImg" TYPE vector(1408);
ALTER TABLE "ShopifyEmbedding" ALTER COLUMN "vectorImg" TYPE vector(1408);

-- HNSW indexes for image-vector kNN.
CREATE INDEX IF NOT EXISTS "idx_pe_vector_img"
  ON "ProductEmbedding" USING hnsw ("vectorImg" vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS "idx_se_vector_img"
  ON "ShopifyEmbedding" USING hnsw ("vectorImg" vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Per-signal scores on ProductMatch so the UI can show text vs image breakdown.
-- imageScore is NULL when fallback to text-only (matchType='semantic').
ALTER TABLE "ProductMatch"
  ADD COLUMN IF NOT EXISTS "textScore"  decimal(5, 2),
  ADD COLUMN IF NOT EXISTS "imageScore" decimal(5, 2);
