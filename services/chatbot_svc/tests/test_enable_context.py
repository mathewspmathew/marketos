import uuid

from sqlalchemy import text

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, CompetitorCandidate, DiscoveryJob,
    ProductUrl, ProductLevelMatch, ScrapedProduct,
)
from services.chatbot_svc.tools.enable_context import resolve_enable_context


def _product_id(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(
            ShopifyProduct.shopDomain == shop
        ).scalar()


def _add_match(s, shop, pid, scraped_product_id, confidence_tier, rejected):
    s.execute(text("""
        INSERT INTO "ProductLevelMatch"
            (id, "shopDomain", "shopifyProductId", "scrapedProductId",
             confidence, "confidenceTier", "reviewStatus", "updatedAt")
        VALUES (:id, :shop, :pid, :spid, :confidence, :tier, :review_status, now())
    """), {
        "id": str(uuid.uuid4()), "shop": shop, "pid": pid, "spid": scraped_product_id,
        "confidence": 0.9, "tier": confidence_tier,
        "review_status": "REJECTED" if rejected else "PENDING",
    })


def test_fresh_when_no_candidates(seed_shop):
    pid = _product_id(seed_shop)
    ctx = resolve_enable_context(seed_shop, pid)
    assert ctx.state == "FRESH"
    assert ctx.competitors_found == 0
    assert ctx.current_query == "Boat Speaker White"


def test_active_when_flag_on(seed_shop):
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.get(ShopifyProduct, pid).dynamicPricingEnabled = True
    ctx = resolve_enable_context(seed_shop, pid)
    assert ctx.state == "ACTIVE"


def test_paused_with_data_when_candidates_exist(seed_shop):
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.add(CompetitorCandidate(
            id=str(uuid.uuid4()), shopDomain=seed_shop, shopifyProductId=pid,
            url="https://x.test/p", domain="x.test", source="serper_search",
            status="SCRAPED",
        ))
    ctx = resolve_enable_context(seed_shop, pid)
    assert ctx.state == "PAUSED_WITH_DATA"
    assert ctx.competitors_found == 1


def test_query_drift_detected(seed_shop):
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.add(CompetitorCandidate(
            id=str(uuid.uuid4()), shopDomain=seed_shop, shopifyProductId=pid,
            url="https://x.test/p", domain="x.test", source="serper_search",
            status="SCRAPED",
        ))
        s.add(DiscoveryJob(
            id=str(uuid.uuid4()), shopDomain=seed_shop, shopifyProductId=pid,
            status="COMPLETED", query="old query",
        ))
        s.get(ShopifyProduct, pid).searchQueryOverride = "new query"
    ctx = resolve_enable_context(seed_shop, pid)
    assert ctx.existing_query == "old query"
    assert ctx.current_query == "new query"
    assert ctx.query_drifted is True


def test_live_matches_excludes_rejected_and_weak(seed_shop):
    pid = _product_id(seed_shop)
    scraped_ids = []
    try:
        with get_db() as s:
            sp = ScrapedProduct(id=str(uuid.uuid4()), shopDomain=seed_shop,
                                domain="x.test", title="comp")
            s.add(sp); s.flush()
            scraped_ids.append(sp.id)
            _add_match(s, seed_shop, pid, sp.id, "LIKELY", False)

            sp2 = ScrapedProduct(id=str(uuid.uuid4()), shopDomain=seed_shop,
                                 domain="y.test", title="comp2")
            s.add(sp2); s.flush()
            scraped_ids.append(sp2.id)
            _add_match(s, seed_shop, pid, sp2.id, "LIKELY", True)

            sp3 = ScrapedProduct(id=str(uuid.uuid4()), shopDomain=seed_shop,
                                 domain="z.test", title="comp3")
            s.add(sp3); s.flush()
            scraped_ids.append(sp3.id)
            _add_match(s, seed_shop, pid, sp3.id, "WEAK", False)
        ctx = resolve_enable_context(seed_shop, pid)
        assert ctx.live_matches == 1
    finally:
        # Clean up rows the seed_shop fixture's teardown doesn't know about
        # (ScrapedProduct has no cascading FK from ShopifyProduct/ShopifyUser).
        with get_db() as s:
            s.execute(text(
                'DELETE FROM "ProductLevelMatch" WHERE "scrapedProductId" = ANY(:ids)'
            ), {"ids": scraped_ids})
            s.query(ScrapedProduct).filter(ScrapedProduct.id.in_(scraped_ids)).delete(
                synchronize_session=False
            )
