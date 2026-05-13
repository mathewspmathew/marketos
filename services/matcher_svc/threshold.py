"""
services/matcher_svc/threshold.py

Per-domain similarity threshold. The HNSW search returns top-K candidates
across one competitor domain; this decides which ones survive.

Hybrid rule:
- empty list           → 1.0 (drops everything)
- single candidate     → max(0.55, score * 0.85)
- low std (<0.05)      → 0.55 floor only — all candidates kept; the domain
                         is high-consistency (e.g. 40 near-duplicates from
                         Zara) and mean-std would needlessly trim Strong
                         clusters.
- otherwise            → clamp(mean - 0.5*std, 0.55, 0.90)

Floor 0.55 = empirical lower bound for "useful" semantic similarity.
Ceiling 0.90 prevents the threshold from rising so high it rejects
slight wording variants on a tightly-clustered domain.
"""
import statistics

FLOOR = 0.55
CEILING = 0.90
LOW_STD_BYPASS = 0.05


def compute_domain_threshold(scores: list[float]) -> float:
    
    # no scores means no candidates, so we return 1.0 to reject everything. We want to be strict when there's no strong evidence.
    if not scores:
        return 1.0
    
    # If there's only one candidate, we can't compute std or mean - apply a simple decay to the single score to be slightly more forgiving, but still reject very low scores.
    if len(scores) == 1:
        return max(FLOOR, scores[0] * 0.85)

    # in normal case we compute mean and std, but if std is very low, we assume all candidates are similarly good and just apply the floor threshold to keep them all.  
    
    # big std means scores are spread out, so we want to be more selective (mean - 0.5*std). Low std means scores are clustered together, so we want to keep them all if they're above the floor.
    std = statistics.stdev(scores)
    if std < LOW_STD_BYPASS:
        return FLOOR

    mean = statistics.mean(scores)
    dynamic = mean - 0.5 * std
    return max(FLOOR, min(CEILING, dynamic))
