"""Discovery pipeline debugging tool.

Queries existing schema (no new columns needed):
- DiscoveryJob: status, error, query, requestedAt, completedAt
- CompetitorCandidate: status (enum)
- ProductMatch: count by competitorProdId
"""
from __future__ import annotations

from sqlalchemy import func

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct,
    DiscoveryJob,
    CompetitorCandidate,
    ProductMatch,
)
from services.chatbot_svc.schemas import (
    DiscoveryDebugInfo,
    CandidateCountByStatus,
)


def debug_discovery(shop_domain: str, product_id: str) -> DiscoveryDebugInfo | None:
    """Return detailed discovery pipeline status for a product.

    Queries: DiscoveryJob, CompetitorCandidate, ProductMatch.
    Returns: candidate counts by status + error details + recommended action.
    """
    with get_db() as s:
        # Get product
        product = s.query(ShopifyProduct).filter(
            ShopifyProduct.id == product_id,
            ShopifyProduct.shopDomain == shop_domain,
        ).first()

        if not product:
            return None

        # Get latest discovery job
        job = (
            s.query(DiscoveryJob)
            .filter(
                DiscoveryJob.shopifyProductId == product_id,
                DiscoveryJob.shopDomain == shop_domain,
            )
            .order_by(DiscoveryJob.requestedAt.desc())
            .first()
        )

        if not job:
            return None

        # Count candidates by status using query counts (more efficient)
        pending_count = (
            s.query(func.count(CompetitorCandidate.id))
            .filter(
                CompetitorCandidate.discoveryJobId == job.id,
                CompetitorCandidate.status == "PENDING",
            )
            .scalar() or 0
        )
        scraped_count = (
            s.query(func.count(CompetitorCandidate.id))
            .filter(
                CompetitorCandidate.discoveryJobId == job.id,
                CompetitorCandidate.status == "SCRAPED",
            )
            .scalar() or 0
        )
        verified_count = (
            s.query(func.count(CompetitorCandidate.id))
            .filter(
                CompetitorCandidate.discoveryJobId == job.id,
                CompetitorCandidate.status == "VERIFIED",
            )
            .scalar() or 0
        )
        rejected_count = (
            s.query(func.count(CompetitorCandidate.id))
            .filter(
                CompetitorCandidate.discoveryJobId == job.id,
                CompetitorCandidate.status == "REJECTED",
            )
            .scalar() or 0
        )
        dead_count = (
            s.query(func.count(CompetitorCandidate.id))
            .filter(
                CompetitorCandidate.discoveryJobId == job.id,
                CompetitorCandidate.status == "DEAD",
            )
            .scalar() or 0
        )

        counts = CandidateCountByStatus(
            pending=pending_count,
            scraped=scraped_count,
            verified=verified_count,
            rejected=rejected_count,
            dead=dead_count,
        )

        total_candidates = (
            pending_count + scraped_count + verified_count + rejected_count + dead_count
        )

        # Get matches from verified candidates
        matched_count = (
            s.query(func.count(ProductMatch.id))
            .join(
                CompetitorCandidate,
                CompetitorCandidate.scrapedProductId == ProductMatch.competitorProdId,
            )
            .filter(
                CompetitorCandidate.discoveryJobId == job.id,
                CompetitorCandidate.status == "VERIFIED",
            )
            .scalar() or 0
        )

        unmatched_verified = verified_count - matched_count

        # Build hint & recommendation
        hint, action = _build_hint_and_action(
            str(job.status),
            counts,
            matched_count,
            job.error,
        )

        # Format timestamps
        requested_at_str = job.requestedAt.isoformat() if job.requestedAt else None
        completed_at_str = job.completedAt.isoformat() if job.completedAt else None

        return DiscoveryDebugInfo(
            product_id=product_id,
            product_title=product.title,
            latest_job_id=job.id,
            job_status=str(job.status),
            query_used=job.query or "(no query stored)",
            num_results_requested=product.discoveryNumResults or 10,
            listing_expansion_cap=product.listingExpansionCap or 0,
            job_requested_at=requested_at_str,
            job_completed_at=completed_at_str,
            candidate_counts=counts,
            total_candidates_found=total_candidates,
            verified_candidates=verified_count,
            matched_variants=matched_count,
            unmatched_verified=unmatched_verified,
            job_error=job.error,
            hint=hint,
            recommended_action=action,
        )


def _build_hint_and_action(
    job_status: str,
    counts: CandidateCountByStatus,
    matched_count: int,
    error: str | None,
) -> tuple[str, str]:
    """Generate human-readable hint and recommended next action."""

    if job_status == "QUEUED":
        return (
            "Discovery is queued but hasn't started yet.",
            "Wait a few seconds, then check again.",
        )

    if job_status == "RUNNING":
        return (
            "Discovery is still running.",
            "Wait for it to complete, then check again.",
        )

    if job_status == "FAILED":
        reason = error or "Unknown error (check logs)"
        return (
            f"Discovery run failed: {reason}",
            "Re-run discovery by disabling and re-enabling this product.",
        )

    # Job completed
    if counts.total_candidates_found == 0:
        return (
            "No competitors found for the search query.",
            "Try a broader query or different keywords. Edit the search query and re-run.",
        )

    if counts.verified == 0:
        dead_or_rejected = counts.dead + counts.rejected
        reasons = []
        if counts.dead > 0:
            reasons.append(f"{counts.dead} dead link(s)")
        if counts.rejected > 0:
            reasons.append(f"{counts.rejected} extraction failure(s)")
        reason_str = " / ".join(reasons) if reasons else "extraction issues"

        return (
            f"Found {counts.total_candidates_found} candidate(s), but all had issues ({reason_str}).",
            "Try a different search query with more specific brand names or product type.",
        )

    if matched_count == 0:
        return (
            f"Found {counts.verified} verified competitor(s), but none matched your variants.",
            "Competitors may be from different markets or product categories. "
            "Review the competitors manually or adjust discovery settings.",
        )

    return (
        f"Discovery complete: {matched_count} competitor(s) matched to your variants.",
        "Pricing is now active. Monitor matches over time.",
    )
