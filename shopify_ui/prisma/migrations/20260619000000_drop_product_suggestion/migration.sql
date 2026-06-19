-- Drop ProductSuggestion table. The suggestion feature has been removed from the codebase:
-- - suggestion-worker service deleted
-- - suggestion_queue removed from celery
-- - ProductSuggestion model removed from schema
-- Table is no longer generated or populated.

DROP TABLE IF EXISTS "ProductSuggestion";
