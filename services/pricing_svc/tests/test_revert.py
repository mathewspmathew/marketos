import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from services.common.db import get_db
from services.common import models
from services.common.shopify_auth import ShopifyAPIError, ShopifyAuthError
from services.pricing_svc.revert import RevertError, revert_price_decision


@pytest.fixture
def seeded_reverted_scenario():
    shop = f"revert-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    decision_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Revert Test Product",
            dynamicPricingEnabled=True,
        ))
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default", currentPrice=90.00,
        ))
        s.flush()
        s.add(models.PriceDecision(
            id=decision_id, shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=100.00, newPrice=90.00, reason="auto price drop",
            appliedAt=datetime.now(timezone.utc),
        ))

    yield shop, product_id, variant_id, decision_id

    with get_db() as s:
        s.query(models.PriceDecision).filter(models.PriceDecision.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


_FAKE_SUCCESS = {
    "data": {"productVariantsBulkUpdate": {
        "productVariants": [{"id": "x", "price": "100.00"}],
        "userErrors": [],
    }}
}


def test_revert_success_pushes_price_and_pauses_product(seeded_reverted_scenario):
    shop, product_id, variant_id, decision_id = seeded_reverted_scenario
    with patch("services.pricing_svc.revert.call_shopify_admin", return_value=_FAKE_SUCCESS):
        with get_db() as s:
            result = revert_price_decision(s, shop, variant_id, decision_id)
    assert result["oldPrice"] == 100.0
    assert result["newPrice"] == 90.0

    with get_db() as s:
        variant = s.get(models.ShopifyVariant, variant_id)
        product = s.get(models.ShopifyProduct, product_id)
        decision = s.get(models.PriceDecision, decision_id)
        audit_rows = s.query(models.PriceDecision).filter(
            models.PriceDecision.shopifyVariantId == variant_id,
            models.PriceDecision.id != decision_id,
        ).all()
        assert float(variant.currentPrice) == 100.0
        assert product.dynamicPricingEnabled is False
        assert decision.revertedAt is not None
        assert len(audit_rows) == 1
        assert "manual_revert" in audit_rows[0].reason


def test_revert_rejects_unknown_variant(seeded_reverted_scenario):
    shop, _, _, decision_id = seeded_reverted_scenario
    with get_db() as s:
        with pytest.raises(RevertError, match="not found"):
            revert_price_decision(s, shop, "nonexistent-variant", decision_id)


def test_revert_rejects_wrong_shop(seeded_reverted_scenario):
    _, _, variant_id, decision_id = seeded_reverted_scenario
    with get_db() as s:
        with pytest.raises(RevertError, match="not found"):
            revert_price_decision(s, "other-shop.myshopify.com", variant_id, decision_id)


def test_revert_rejects_decision_not_found(seeded_reverted_scenario):
    shop, _, variant_id, _ = seeded_reverted_scenario
    with get_db() as s:
        with pytest.raises(RevertError, match="not found"):
            revert_price_decision(s, shop, variant_id, "nonexistent-decision")


def test_revert_rejects_never_applied_decision(seeded_reverted_scenario):
    shop, _, variant_id, _ = seeded_reverted_scenario
    unapplied_id = str(uuid.uuid4())
    with get_db() as s:
        s.add(models.PriceDecision(
            id=unapplied_id, shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=100.00, newPrice=90.00, reason="pending, never applied",
        ))
    with get_db() as s:
        with pytest.raises(RevertError, match="not been applied"):
            revert_price_decision(s, shop, variant_id, unapplied_id)


def test_revert_rejects_already_reverted_decision(seeded_reverted_scenario):
    shop, _, variant_id, decision_id = seeded_reverted_scenario
    with get_db() as s:
        d = s.get(models.PriceDecision, decision_id)
        d.revertedAt = datetime.now(timezone.utc)
    with get_db() as s:
        with pytest.raises(RevertError, match="already been reverted"):
            revert_price_decision(s, shop, variant_id, decision_id)


def test_revert_no_partial_write_on_shopify_auth_error(seeded_reverted_scenario):
    shop, product_id, variant_id, decision_id = seeded_reverted_scenario
    with patch("services.pricing_svc.revert.call_shopify_admin", side_effect=ShopifyAuthError("no offline session")):
        with get_db() as s:
            with pytest.raises(RevertError):
                revert_price_decision(s, shop, variant_id, decision_id)

    with get_db() as s:
        variant = s.get(models.ShopifyVariant, variant_id)
        product = s.get(models.ShopifyProduct, product_id)
        decision = s.get(models.PriceDecision, decision_id)
        assert float(variant.currentPrice) == 90.0  # unchanged
        assert product.dynamicPricingEnabled is True  # unchanged
        assert decision.revertedAt is None  # unchanged


def test_revert_no_partial_write_on_shopify_user_errors(seeded_reverted_scenario):
    shop, product_id, variant_id, decision_id = seeded_reverted_scenario
    fake_failure = {
        "data": {"productVariantsBulkUpdate": {
            "productVariants": [],
            "userErrors": [{"field": "price", "message": "Price is invalid"}],
        }}
    }
    with patch("services.pricing_svc.revert.call_shopify_admin", return_value=fake_failure):
        with get_db() as s:
            with pytest.raises(RevertError, match="Price is invalid"):
                revert_price_decision(s, shop, variant_id, decision_id)

    with get_db() as s:
        variant = s.get(models.ShopifyVariant, variant_id)
        decision = s.get(models.PriceDecision, decision_id)
        assert float(variant.currentPrice) == 90.0
        assert decision.revertedAt is None
