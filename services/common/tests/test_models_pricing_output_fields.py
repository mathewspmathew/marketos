"""Column-mapping smoke test for the PriceDecision / VariantCompetitorStats
SQLAlchemy models — confirms they load and match the live Postgres schema
(added for delete_dynamic_pricing's use, see
docs/superpowers/specs/2026-07-13-pause-delete-dynamic-pricing-design.md).
"""
import uuid
from datetime import datetime, timezone

from services.common.db import get_db
from services.common import models


def test_price_decision_and_variant_competitor_stats_round_trip():
    shop = f"pd-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    decision_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Test Product",
            dynamicPricingEnabled=False,
        ))
        s.flush()
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default Title",
            currentPrice=19.99,
        ))
        s.flush()
        s.add(models.VariantCompetitorStats(
            shopifyVariantId=variant_id, shopDomain=shop,
            competitorCount=3, minPrice=15.00, median=18.00, maxPrice=22.00,
        ))
        s.add(models.PriceDecision(
            id=decision_id, shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=19.99, newPrice=17.99, reason="undercut_competitor",
            tierAtDecision="COMPETITIVE", autoApplied=True,
        ))

    try:
        with get_db() as s:
            stats = s.get(models.VariantCompetitorStats, variant_id)
            assert stats is not None
            assert stats.competitorCount == 3
            assert float(stats.minPrice) == 15.00

            decision = s.get(models.PriceDecision, decision_id)
            assert decision is not None
            assert float(decision.oldPrice) == 19.99
            assert float(decision.newPrice) == 17.99
            assert decision.tierAtDecision == "COMPETITIVE"
            assert decision.autoApplied is True
    finally:
        with get_db() as s:
            s.query(models.PriceDecision).filter(models.PriceDecision.id == decision_id).delete(synchronize_session=False)
            s.query(models.VariantCompetitorStats).filter(models.VariantCompetitorStats.shopifyVariantId == variant_id).delete(synchronize_session=False)
            s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
            s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
            s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)
