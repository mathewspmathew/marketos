"""Integration tests for delete_dynamic_pricing against the real dev DB.

Covers: full cleanup of a product's own (non-shared) competitor/pricing
data, the shared-ScrapedProduct guard (a legacy pre-isolation-fix scenario —
constructed directly via the models, since new scrapes can no longer
produce it), and ScrapingConfig cleanup.
"""
import uuid

import pytest

from services.common.db import get_db
from services.common import models
from services.common.pane_config import delete_dynamic_pricing


def _make_shop_and_product(shop, product_id, *, dynamic_pricing_enabled=True):
    with get_db() as s:
        existing = s.get(models.ShopifyUser, shop)
        if not existing:
            s.add(models.ShopifyUser(shopDomain=shop))
            s.flush()
        s.add(models.ShopifyProduct(
            id=product_id,
            shopDomain=shop,
            title="Test Product",
            dynamicPricingEnabled=dynamic_pricing_enabled,
            pricingTier="PREMIUM",
            minPriceOverride=800,
            maxPriceOverride=1200,
            frequencyUnit="hour",
            frequencyInterval=6,
            searchQueryOverride="test query",
            listingExpansionCap=20,
            discoveryNumResults=15,
            avgBasePrice=1000,
        ))


def _cleanup_all(shop, product_ids, variant_ids, scraped_ids, config_ids):
    with get_db() as s:
        s.query(models.VariantCompetitorStats).filter(models.VariantCompetitorStats.shopifyVariantId.in_(variant_ids)).delete(synchronize_session=False)
        s.query(models.PriceDecision).filter(models.PriceDecision.shopifyVariantId.in_(variant_ids)).delete(synchronize_session=False)
        s.query(models.ProductMatch).filter(models.ProductMatch.shopifyVariantId.in_(variant_ids)).delete(synchronize_session=False)
        s.query(models.ProductLevelMatch).filter(models.ProductLevelMatch.shopifyProductId.in_(product_ids)).delete(synchronize_session=False)
        s.query(models.CompetitorCandidate).filter(models.CompetitorCandidate.shopifyProductId.in_(product_ids)).delete(synchronize_session=False)
        s.query(models.DiscoveryJob).filter(models.DiscoveryJob.shopifyProductId.in_(product_ids)).delete(synchronize_session=False)
        s.query(models.ProductUrl).filter(models.ProductUrl.shopifyProductId.in_(product_ids)).delete(synchronize_session=False)
        if config_ids:
            s.query(models.ScrapingConfig).filter(models.ScrapingConfig.id.in_(config_ids)).delete(synchronize_session=False)
        s.query(models.ScrapedVariant).filter(models.ScrapedVariant.productId.in_(scraped_ids)).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id.in_(scraped_ids)).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.productId.in_(product_ids)).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id.in_(product_ids)).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_delete_removes_owned_data_and_resets_fields():
    shop = f"del-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    scraped_id = str(uuid.uuid4())
    url_id = str(uuid.uuid4())
    cand_id = str(uuid.uuid4())
    match_id = str(uuid.uuid4())
    level_match_id = str(uuid.uuid4())
    stats_variant_id = variant_id

    _make_shop_and_product(shop, product_id)
    with get_db() as s:
        s.add(models.ShopifyVariant(id=variant_id, productId=product_id, title="Default Title", currentPrice=19.99))
        s.add(models.ScrapedProduct(id=scraped_id, shopDomain=shop, domain="example.com", title="Competitor"))
        s.flush()
        s.add(models.ProductUrl(
            id=url_id, shopDomain=shop, shopifyProductId=product_id, prodId=scraped_id,
            url=f"https://example.com/{url_id}", status="ACTIVE",
        ))
        s.add(models.CompetitorCandidate(
            id=cand_id, shopDomain=shop, shopifyProductId=product_id,
            url=f"https://example.com/{cand_id}", domain="example.com", source="serper",
            scrapedProductId=scraped_id,
        ))
        s.add(models.VariantCompetitorStats(shopifyVariantId=stats_variant_id, shopDomain=shop, competitorCount=1))
        s.add(models.PriceDecision(
            id=str(uuid.uuid4()), shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=19.99, newPrice=17.99, reason="test",
        ))
        s.add(models.ProductLevelMatch(
            id=level_match_id, shopDomain=shop, shopifyProductId=product_id,
            scrapedProductId=scraped_id, confidenceTier="CONFIRMED",
        ))

    try:
        with get_db() as s:
            product = s.get(models.ShopifyProduct, product_id)
            result = delete_dynamic_pricing(s, product)
            assert result == {"deletedScrapedProducts": 1}

        with get_db() as s:
            product = s.get(models.ShopifyProduct, product_id)
            assert product.dynamicPricingEnabled is False
            assert product.frequencyInterval is None
            assert product.frequencyUnit is None
            assert product.pricingTier == "COMPETITIVE"
            assert product.minPriceOverride is None
            assert product.maxPriceOverride is None
            assert product.searchQueryOverride is None
            assert product.listingExpansionCap is None
            assert product.discoveryNumResults is None
            assert product.avgBasePrice is None

            assert s.get(models.ScrapedProduct, scraped_id) is None
            assert s.get(models.ProductUrl, url_id) is None
            assert s.get(models.CompetitorCandidate, cand_id) is None
            assert s.get(models.ProductLevelMatch, level_match_id) is None
            assert s.get(models.VariantCompetitorStats, stats_variant_id) is None
            assert s.query(models.PriceDecision).filter(models.PriceDecision.shopifyVariantId == variant_id).count() == 0
    finally:
        _cleanup_all(shop, [product_id], [variant_id], [scraped_id], [])


def test_delete_guards_scraped_product_shared_with_another_product():
    shop = f"del-guard-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_a_id = f"product-a-{uuid.uuid4().hex[:8]}"
    product_b_id = f"product-b-{uuid.uuid4().hex[:8]}"
    variant_a_id = f"variant-a-{uuid.uuid4().hex[:8]}"
    variant_b_id = f"variant-b-{uuid.uuid4().hex[:8]}"
    shared_scraped_id = str(uuid.uuid4())
    cand_a_id = str(uuid.uuid4())
    cand_b_id = str(uuid.uuid4())

    _make_shop_and_product(shop, product_a_id)
    _make_shop_and_product(shop, product_b_id)
    with get_db() as s:
        s.add(models.ShopifyVariant(id=variant_a_id, productId=product_a_id, title="A", currentPrice=10.0))
        s.add(models.ShopifyVariant(id=variant_b_id, productId=product_b_id, title="B", currentPrice=20.0))
        # A legacy pre-isolation-fix shared ScrapedProduct: both products'
        # CompetitorCandidate rows point at the same row. New scrapes can no
        # longer produce this state — constructed directly for this test.
        s.add(models.ScrapedProduct(id=shared_scraped_id, shopDomain=shop, domain="shared.example.com", title="Shared Competitor"))
        s.flush()
        s.add(models.CompetitorCandidate(
            id=cand_a_id, shopDomain=shop, shopifyProductId=product_a_id,
            url="https://shared.example.com/item", domain="shared.example.com", source="serper",
            scrapedProductId=shared_scraped_id,
        ))
        s.add(models.CompetitorCandidate(
            id=cand_b_id, shopDomain=shop, shopifyProductId=product_b_id,
            url="https://shared.example.com/item", domain="shared.example.com", source="serper",
            scrapedProductId=shared_scraped_id,
        ))

    try:
        with get_db() as s:
            product_a = s.get(models.ShopifyProduct, product_a_id)
            result = delete_dynamic_pricing(s, product_a)
            assert result == {"deletedScrapedProducts": 0}, "guard must hold: product B still references this ScrapedProduct"

        with get_db() as s:
            assert s.get(models.ScrapedProduct, shared_scraped_id) is not None, "shared row must survive"
            assert s.get(models.CompetitorCandidate, cand_a_id) is None, "product A's own candidate row is still deleted"
            assert s.get(models.CompetitorCandidate, cand_b_id) is not None, "product B's candidate row is untouched"
    finally:
        with get_db() as s:
            s.query(models.CompetitorCandidate).filter(models.CompetitorCandidate.id.in_([cand_a_id, cand_b_id])).delete(synchronize_session=False)
            s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == shared_scraped_id).delete(synchronize_session=False)
            s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id.in_([variant_a_id, variant_b_id])).delete(synchronize_session=False)
            s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id.in_([product_a_id, product_b_id])).delete(synchronize_session=False)
            s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_delete_removes_referenced_scraping_config():
    shop = f"del-cfg-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    config_id = str(uuid.uuid4())
    url_id = str(uuid.uuid4())
    scraped_id = str(uuid.uuid4())

    _make_shop_and_product(shop, product_id)
    with get_db() as s:
        s.add(models.ScrapedProduct(id=scraped_id, shopDomain=shop, domain="example.com", title="Competitor"))
        s.add(models.ScrapingConfig(id=config_id, shopDomain=shop, competitorUrl="https://example.com"))
        s.flush()
        s.add(models.ProductUrl(
            id=url_id, shopDomain=shop, shopifyProductId=product_id, prodId=scraped_id,
            configId=config_id, url=f"https://example.com/{url_id}", status="ACTIVE",
        ))

    try:
        with get_db() as s:
            product = s.get(models.ShopifyProduct, product_id)
            delete_dynamic_pricing(s, product)

        with get_db() as s:
            assert s.get(models.ScrapingConfig, config_id) is None
    finally:
        _cleanup_all(shop, [product_id], [], [scraped_id], [config_id])
