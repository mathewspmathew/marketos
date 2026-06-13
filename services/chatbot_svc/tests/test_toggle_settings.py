# services/chatbot_svc/tests/test_toggle_settings.py
import os
os.environ.setdefault("GROQ_API_KEY", "test")

import uuid

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, ScrapedProduct, CompetitorCandidate, ProductUrl,
)
from services.chatbot_svc.tools.toggle_settings import (
    resolve_enable_settings, compute_disable_counts,
)


def _product_id(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(ShopifyProduct.shopDomain == shop).scalar()


def test_resolve_enable_settings_uses_fallback_defaults(seed_shop):
    pid = _product_id(seed_shop)
    out = resolve_enable_settings(seed_shop, [pid])
    assert out["productId"] == pid
    # seed product: discoveryNumResults defaults to 10, listingExpansionCap is null,
    # no ShopSettings row -> 5. searchQuery null -> falls back to the title.
    assert out["numResults"] == 10
    assert out["listingExpansionCap"] == 5
    assert out["query"] == "Boat Speaker White"


def test_resolve_enable_settings_empty_products():
    out = resolve_enable_settings("nobody.myshopify.com", [])
    assert out == {"productId": None, "numResults": 10, "listingExpansionCap": 5, "query": ""}


def test_compute_disable_counts_with_shared_row_guard(seed_shop):
    pid = _product_id(seed_shop)
    sp_solo = f"sp-solo-{uuid.uuid4().hex[:8]}"
    sp_shared = f"sp-shared-{uuid.uuid4().hex[:8]}"
    cand_ids = []
    scraped_ids = [sp_solo, sp_shared]
    try:
        with get_db() as s:
            s.add(ScrapedProduct(id=sp_solo, shopDomain=seed_shop, domain="a.com", title="Solo Product"))
            s.add(ScrapedProduct(id=sp_shared, shopDomain=seed_shop, domain="b.com", title="Shared Product"))
            s.flush()
            # P's candidates: one -> solo scraped, one -> shared scraped, one un-scraped (PENDING).
            for sp in (sp_solo, sp_shared, None):
                cid = uuid.uuid4().hex
                cand_ids.append(cid)
                s.add(CompetitorCandidate(
                    id=cid, shopDomain=seed_shop, shopifyProductId=pid,
                    url=f"https://x.com/{cid}", domain="x.com", source="test",
                    status="SCRAPED" if sp else "PENDING", scrapedProductId=sp,
                ))
            # A SECOND product in the same shop also references sp_shared -> guard keeps it.
            other_pid = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
            s.add(ShopifyProduct(id=other_pid, shopDomain=seed_shop, title="Other",
                                 vendor="Boat", productType="audio", tags=[],
                                 dynamicPricingEnabled=True))
            s.flush()
            cid2 = uuid.uuid4().hex
            cand_ids.append(cid2)
            s.add(CompetitorCandidate(
                id=cid2, shopDomain=seed_shop, shopifyProductId=other_pid,
                url="https://x.com/shared-ref", domain="x.com", source="test",
                status="SCRAPED", scrapedProductId=sp_shared,
            ))

        counts = compute_disable_counts(seed_shop, pid)
        # 3 candidates belong to P (solo, shared, pending).
        assert counts["discovered_links"] == 3
        # Only the solo scraped product is deletable; shared is referenced by other_pid.
        assert counts["competitor_products"] == 1
        # seed_shop's target product has exactly 1 variant.
        assert counts["price_stats_variants"] == 1
    finally:
        with get_db() as s:
            s.query(CompetitorCandidate).filter(
                CompetitorCandidate.id.in_(cand_ids)
            ).delete(synchronize_session=False)
            s.query(ScrapedProduct).filter(
                ScrapedProduct.id.in_(scraped_ids)
            ).delete(synchronize_session=False)
