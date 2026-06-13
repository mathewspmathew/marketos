ALTER TABLE "ChatMessage"
  ADD COLUMN "tokenCount" INTEGER,
  ADD COLUMN "pinned" BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE "ChatSession"
  ADD COLUMN "runningSummary" TEXT;

CREATE INDEX "ChatMessage_sessionId_pinned_idx"
  ON "ChatMessage" ("sessionId", "pinned")
  WHERE "pinned" = TRUE;
