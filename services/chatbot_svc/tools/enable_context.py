from __future__ import annotations

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, CompetitorCandidate, DiscoveryJob,
    ProductUrl, ProductLevelMatch,
)
from services.chatbot_svc.schemas import EnableContext
from services.chatbot_svc.tools.toggle_settings import resolve_enable_settings


def resolve_enable_context(shop_domain: str, product_id: str) -> EnableContext | None:
    """Derive the enable-card context for one product. Read-only.

    Returns None if the product does not belong to shop_domain.
    """
    settings = resolve_enable_settings(shop_domain, [product_id])

    with get_db() as s:
        p = s.get(ShopifyProduct, product_id)
        if p is None or p.shopDomain != shop_domain:
            return None

        competitors_found = (
            s.query(CompetitorCandidate)
            .filter(CompetitorCandidate.shopDomain == shop_domain,
                    CompetitorCandidate.shopifyProductId == product_id)
            .count()
        )
        live_matches = (
            s.query(ProductLevelMatch)
            .filter(ProductLevelMatch.shopDomain == shop_domain,
                    ProductLevelMatch.shopifyProductId == product_id,
                    ProductLevelMatch.reviewStatus != "REJECTED",
                    ProductLevelMatch.confidenceTier.in_(("CONFIRMED", "LIKELY")))
            .count()
        )
        dead_links = (
            s.query(ProductUrl)
            .filter(ProductUrl.shopifyProductId == product_id,
                    ProductUrl.status == "DEAD")
            .count()
        )
        latest_job = (
            s.query(DiscoveryJob)
            .filter(DiscoveryJob.shopDomain == shop_domain,
                    DiscoveryJob.shopifyProductId == product_id)
            .order_by(DiscoveryJob.requestedAt.desc())
            .first()
        )
        existing_query = latest_job.query if latest_job else None
        last = p.lastDiscoveryAt or (latest_job.completedAt if latest_job else None)
        enabled = bool(p.dynamicPricingEnabled)

    current_query = settings["query"]
    query_drifted = bool(existing_query) and existing_query != current_query

    if enabled:
        state = "ACTIVE"
    elif competitors_found == 0:
        state = "FRESH"
    else:
        state = "PAUSED_WITH_DATA"

    return EnableContext(
        product_id=product_id,
        state=state,
        competitors_found=competitors_found,
        live_matches=live_matches,
        last_discovery_at=last.isoformat() if last else None,
        existing_query=existing_query,
        current_query=current_query,
        query_drifted=query_drifted,
        dead_links=dead_links,
        num_results=settings["numResults"],
        listing_expansion_cap=settings["listingExpansionCap"],
    )
