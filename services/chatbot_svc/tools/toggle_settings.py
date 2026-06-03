# services/chatbot_svc/tools/toggle_settings.py
"""DB helpers for the chatbot dynamic-pricing toggle brief (Feature A).

resolve_enable_settings — per-product scrape-setting defaults shown on the
enable card. compute_disable_counts — what the delete path would remove (with
the shared-ScrapedProduct guard), shown on the disable card.

The set-resolution rule in compute_disable_counts MUST stay identical to
shopify_ui/app/lib/competitorTeardown.server.js, which performs the actual
delete in Prisma.
"""
from __future__ import annotations

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, ShopifyVariant, ShopSettings,
    CompetitorCandidate, ProductUrl,
)

DEFAULT_NUM_RESULTS = 10
DEFAULT_LISTING_CAP = 5


def resolve_enable_settings(shop_domain: str, product_ids: list[str]) -> dict:
    """Resolve the scrape settings to pre-fill the enable card for the (first)
    product. Fallback chains mirror app.discover.$id.jsx."""
    if not product_ids:
        return {"productId": None, "numResults": DEFAULT_NUM_RESULTS,
                "listingExpansionCap": DEFAULT_LISTING_CAP, "query": ""}
    pid = product_ids[0]
    with get_db() as s:
        p = s.get(ShopifyProduct, pid)
        settings = s.get(ShopSettings, shop_domain)
        if p is None:
            return {"productId": pid, "numResults": DEFAULT_NUM_RESULTS,
                    "listingExpansionCap": DEFAULT_LISTING_CAP, "query": ""}
        num = (p.discoveryNumResults
               if p.discoveryNumResults is not None else DEFAULT_NUM_RESULTS)
        if p.listingExpansionCap is not None:
            cap = p.listingExpansionCap
        elif settings is not None and settings.listingExpansionCap is not None:
            cap = settings.listingExpansionCap
        else:
            cap = DEFAULT_LISTING_CAP
        query = (p.searchQueryOverride or p.searchQuery or p.title or "")
    return {"productId": pid, "numResults": num,
            "listingExpansionCap": cap, "query": query}
