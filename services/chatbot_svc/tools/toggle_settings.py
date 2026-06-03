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


def compute_disable_counts(shop_domain: str, product_id: str) -> dict:
    """Counts shown on the disable card's delete option.

    competitor_products: distinct ScrapedProducts linked to this product (via
      its candidates' scrapedProductId and its ProductUrls' prodId) that are
      NOT referenced by any other product (shared-row guard).
    discovered_links: this product's CompetitorCandidate rows.
    price_stats_variants: this product's ShopifyVariant count (proxy for the
      VariantCompetitorStats / PriceDecision rows the delete clears).
    """
    with get_db() as s:
        cand_rows = (
            s.query(CompetitorCandidate.scrapedProductId)
            .filter(CompetitorCandidate.shopDomain == shop_domain,
                    CompetitorCandidate.shopifyProductId == product_id)
            .all()
        )
        discovered_links = len(cand_rows)
        scraped_from_cands = {r.scrapedProductId for r in cand_rows if r.scrapedProductId}

        url_rows = (
            s.query(ProductUrl.prodId)
            .filter(ProductUrl.shopDomain == shop_domain,
                    ProductUrl.shopifyProductId == product_id)
            .all()
        )
        scraped_from_urls = {r.prodId for r in url_rows if r.prodId}
        my_scraped = scraped_from_cands | scraped_from_urls

        deletable = 0
        for sid in my_scraped:
            other_cand = (
                s.query(CompetitorCandidate.id)
                .filter(CompetitorCandidate.scrapedProductId == sid,
                        CompetitorCandidate.shopifyProductId != product_id)
                .first()
            )
            other_url = (
                s.query(ProductUrl.id)
                .filter(ProductUrl.prodId == sid,
                        ProductUrl.shopifyProductId != product_id)
                .first()
            )
            if other_cand is None and other_url is None:
                deletable += 1

        variant_count = (
            s.query(ShopifyVariant.id)
            .filter(ShopifyVariant.productId == product_id)
            .count()
        )

    return {
        "competitor_products": deletable,
        "discovered_links": discovered_links,
        "price_stats_variants": variant_count,
    }
