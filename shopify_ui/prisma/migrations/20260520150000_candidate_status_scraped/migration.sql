-- Add SCRAPED terminal state to CandidateStatus.
-- Successful extract_candidate runs now mark the candidate SCRAPED so the
-- Discover UI badge reflects that the row is fully scraped + embedded,
-- distinct from PENDING (still in flight) and VERIFIED (legacy gate).
ALTER TYPE "CandidateStatus" ADD VALUE IF NOT EXISTS 'SCRAPED' AFTER 'PENDING';
