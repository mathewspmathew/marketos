import uuid
from datetime import datetime, timezone

from services.common.db import get_db
from services.common.models import PriceDecision, ShopifyProduct, ShopifyUser, ShopifyVariant


def test_price_decision_reverted_at_defaults_to_none_and_round_trips():
    shop = f"revert-migration-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    decision_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(ShopifyProduct(id=product_id, shopDomain=shop, title="Test", dynamicPricingEnabled=False))
        s.add(ShopifyVariant(id=variant_id, productId=product_id, title="Default", currentPrice=100.00))
        s.flush()
        s.add(PriceDecision(
            id=decision_id, shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=100.00, newPrice=90.00, reason="test",
        ))

    with get_db() as s:
        d = s.get(PriceDecision, decision_id)
        assert d.revertedAt is None

    with get_db() as s:
        d = s.get(PriceDecision, decision_id)
        d.revertedAt = datetime.now(timezone.utc)

    with get_db() as s:
        d = s.get(PriceDecision, decision_id)
        assert d.revertedAt is not None

    with get_db() as s:
        s.query(PriceDecision).filter(PriceDecision.id == decision_id).delete(synchronize_session=False)
        s.query(ShopifyVariant).filter(ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(ShopifyProduct).filter(ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(ShopifyUser).filter(ShopifyUser.shopDomain == shop).delete(synchronize_session=False)
