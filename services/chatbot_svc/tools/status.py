from __future__ import annotations

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, ShopifyVariant, DiscoveryJob, CompetitorCandidate, ProductMatch,
)
from services.chatbot_svc.schemas import DynamicPricingStatus


def _detail(status: str, competitors: int, matches: int, *, job_failed: bool = False) -> str:
    if status == "OFF":
        if competitors > 0:
            return (
                f"Dynamic pricing is off, but {competitors} competitor(s) from a "
                "previous run are still saved — this product can be RESUMED "
                "(it is NOT a first-time setup)."
            )
        return (
            "Dynamic pricing is off for this product; no competitors are saved "
            "yet (first-time setup)."
        )
    if status == "SETTING_UP":
        return "Dynamic pricing is on; setup just started (no competitor discovery run yet)."
    if status == "DISCOVERING":
        return f"Dynamic pricing is on — discovering competitors now ({competitors} found so far)."
    if status == "PROCESSING":
        return (
            f"Dynamic pricing is on — {competitors} competitor(s) found, "
            "matching and pricing in progress."
        )
    if status == "READY":
        return f"Dynamic pricing is active — {matches} competitor match(es) ready."
    # NEEDS_ATTENTION — distinguish a failed run from a run that found nothing.
    if job_failed:
        return (
            "Dynamic pricing is on, but the last competitor-discovery run failed. "
            "You can re-run discovery to try again."
        )
    return (
        "Dynamic pricing is on but no competitors were found — the search query may be "
        "too broad. Consider re-running discovery."
    )


def get_dynamic_pricing_status(shop_domain: str, product_id: str) -> DynamicPricingStatus | None:
    """Derive a single merchant-facing dynamic-pricing status for one product.

    Read-only. Returns None if the product does not belong to shop_domain.
    """
    with get_db() as s:
        p = s.get(ShopifyProduct, product_id)
        if p is None or p.shopDomain != shop_domain:
            return None

        competitors_found = (
            s.query(CompetitorCandidate)
            .filter(
                CompetitorCandidate.shopDomain == shop_domain,
                CompetitorCandidate.shopifyProductId == product_id,
            )
            .count()
        )
        matches = (
            s.query(ProductMatch)
            .join(ShopifyVariant, ShopifyVariant.id == ProductMatch.shopifyVariantId)
            .filter(
                ShopifyVariant.productId == product_id,
                ProductMatch.shopDomain == shop_domain,
            )
            .count()
        )
        job = (
            s.query(DiscoveryJob)
            .filter(
                DiscoveryJob.shopDomain == shop_domain,
                DiscoveryJob.shopifyProductId == product_id,
            )
            .order_by(DiscoveryJob.requestedAt.desc())
            .first()
        )

        if not p.dynamicPricingEnabled:
            status = "OFF"
        elif matches > 0:
            status = "READY"
        elif job is None:
            status = "SETTING_UP"
        elif job.status in ("QUEUED", "RUNNING"):
            status = "DISCOVERING"
        elif job.status == "FAILED":
            status = "NEEDS_ATTENTION"
        elif job.status == "COMPLETED" and (job.candidateCount or 0) == 0 and competitors_found == 0:
            status = "NEEDS_ATTENTION"
        else:
            status = "PROCESSING"

        last = p.lastDiscoveryAt or (job.completedAt if job else None)
        return DynamicPricingStatus(
            product_id=product_id,
            title=p.title,
            status=status,
            competitors_found=competitors_found,
            matches=matches,
            last_discovery_at=last.isoformat() if last else None,
            detail=_detail(
                status,
                competitors_found,
                matches,
                job_failed=(job is not None and job.status == "FAILED"),
            ),
        )
