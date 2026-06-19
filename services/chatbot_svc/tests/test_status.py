from datetime import datetime, timezone

import pytest

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, ShopifyVariant, DiscoveryJob, CompetitorCandidate,
    ProductMatch, ScrapedProduct,
)
from services.chatbot_svc.tools.status import get_dynamic_pricing_status


def _product_id(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(ShopifyProduct.shopDomain == shop).scalar()


def _enable(pid):
    with get_db() as s:
        s.get(ShopifyProduct, pid).dynamicPricingEnabled = True


def test_status_off(seed_shop):
    st = get_dynamic_pricing_status(seed_shop, _product_id(seed_shop))
    assert st.status == "OFF"
    assert "first-time setup" in st.detail.lower()


def test_status_off_with_saved_competitors_says_resume(seed_shop):
    import uuid
    pid = _product_id(seed_shop)
    with get_db() as s:
        s.add(CompetitorCandidate(
            id=str(uuid.uuid4()), shopDomain=seed_shop, shopifyProductId=pid,
            url="https://x.test/p", domain="x.test", source="serper_search",
            status="SCRAPED",
        ))
    st = get_dynamic_pricing_status(seed_shop, pid)
    assert st.status == "OFF"
    assert st.competitors_found == 1
    # OFF + saved competitors must read as a resume, explicitly negating
    # first-time setup so the agent picks the resume question.
    assert "resumed" in st.detail.lower()
    assert "not a first-time" in st.detail.lower()


def test_status_setting_up(seed_shop):
    pid = _product_id(seed_shop); _enable(pid)
    assert get_dynamic_pricing_status(seed_shop, pid).status == "SETTING_UP"


def test_status_discovering(seed_shop):
    pid = _product_id(seed_shop); _enable(pid)
    with get_db() as s:
        s.add(DiscoveryJob(shopDomain=seed_shop, shopifyProductId=pid, status="RUNNING"))
    assert get_dynamic_pricing_status(seed_shop, pid).status == "DISCOVERING"


def test_status_needs_attention_failed(seed_shop):
    pid = _product_id(seed_shop); _enable(pid)
    with get_db() as s:
        s.add(DiscoveryJob(shopDomain=seed_shop, shopifyProductId=pid, status="FAILED"))
    st = get_dynamic_pricing_status(seed_shop, pid)
    assert st.status == "NEEDS_ATTENTION"
    # A failed run must NOT be described as "no competitors found".
    assert "failed" in st.detail.lower()


def test_status_needs_attention_no_competitors(seed_shop):
    pid = _product_id(seed_shop); _enable(pid)
    with get_db() as s:
        job = DiscoveryJob(shopDomain=seed_shop, shopifyProductId=pid,
                           status="COMPLETED")
        s.add(job)
        s.flush()  # Flush to get the job ID
    st = get_dynamic_pricing_status(seed_shop, pid)
    assert st.status == "NEEDS_ATTENTION"
    assert "no competitors" in st.detail.lower()


def test_status_processing(seed_shop):
    import uuid
    pid = _product_id(seed_shop); _enable(pid)
    with get_db() as s:
        job = DiscoveryJob(shopDomain=seed_shop, shopifyProductId=pid,
                           status="COMPLETED")
        s.add(job)
        s.flush()  # Flush to get the job ID
        s.add(CompetitorCandidate(id=str(uuid.uuid4()), discoveryJobId=job.id,
                                  shopDomain=seed_shop, shopifyProductId=pid,
                                  url="https://comp.example/p", domain="comp.example",
                                  source="serper_search", status="PENDING"))
    st = get_dynamic_pricing_status(seed_shop, pid)
    assert st.status == "PROCESSING"
    assert st.competitors_found >= 1


def test_status_ready(seed_shop):
    pid = _product_id(seed_shop); _enable(pid)
    with get_db() as s:
        vid = s.query(ShopifyVariant.id).filter(ShopifyVariant.productId == pid).scalar()
        sp = ScrapedProduct(shopDomain=seed_shop, domain="comp.example", title="Comp")
        s.add(sp); s.flush()
        scraped_id = sp.id
        s.add(ProductMatch(shopDomain=seed_shop, shopifyVariantId=vid,
                           competitorProdId=scraped_id, matchScore=0.9,
                           vectorDistance=0.1, thresholdUsed=0.5))
    try:
        st = get_dynamic_pricing_status(seed_shop, pid)
        assert st.status == "READY"
        assert st.matches >= 1
    finally:
        with get_db() as s:
            s.query(ScrapedProduct).filter(ScrapedProduct.id == scraped_id).delete(
                synchronize_session=False
            )


def test_status_other_shop_returns_none(seed_shop, seed_other_shop):
    pid = _product_id(seed_shop)
    assert get_dynamic_pricing_status(seed_other_shop, pid) is None
