import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from services.common.db import get_db
from services.common import models
from services.shopify_svc.main import handle_product_update


@pytest.fixture
def seeded_product():
    shop = f"webhook-update-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    numeric_id = uuid.uuid4().int % 1_000_000_000
    product_id = f"gid://shopify/Product/{numeric_id}"
    numeric_variant_id = uuid.uuid4().int % 1_000_000_000
    variant_id = f"gid://shopify/ProductVariant/{numeric_variant_id}"

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopSettings(shopDomain=shop, lifetimeCapPct=0.25))
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Webhook Test Product",
            avgBasePrice=100.00,
        ))
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default",
            currentPrice=100.00, basePrice=100.00,
        ))

    yield shop, numeric_id, product_id, numeric_variant_id, variant_id

    with get_db() as s:
        s.query(models.PriceDecision).filter(models.PriceDecision.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.ProductMatch).filter(models.ProductMatch.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopSettings).filter(models.ShopSettings.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def _payload(numeric_id, numeric_variant_id, price, **overrides):
    base = {
        "id": numeric_id,
        "title": "Webhook Test Product",
        "body_html": "",
        "vendor": "TestBrand",
        "product_type": "Widget",
        "handle": "webhook-test-product",
        "status": "active",
        "tags": "a, b",
        "image": {"src": "https://img/p.jpg"},
        "variants": [{
            "id": numeric_variant_id,
            "title": "Default",
            "price": f"{price:.2f}",
            "compare_at_price": None,
            "sku": "SKU1",
            "barcode": None,
            "option1": None, "option2": None, "option3": None,
            "inventory_quantity": 5,
        }],
    }
    base.update(overrides)
    return base


def test_engine_write_back_is_not_flagged_as_manual_edit(seeded_product):
    shop, numeric_id, product_id, numeric_variant_id, variant_id = seeded_product
    with get_db() as s:
        s.add(models.PriceDecision(
            shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=100.00, newPrice=90.00, reason="auto price drop",
            appliedAt=datetime.now(timezone.utc), autoApplied=True,
        ))

    result = handle_product_update(shop, _payload(numeric_id, numeric_variant_id, 90.00))

    assert result["ok"] is True
    assert result["manual_edit"] is False
    with get_db() as s:
        variant = s.get(models.ShopifyVariant, variant_id)
        assert float(variant.basePrice) == 100.00  # unchanged — not re-anchored


def test_manual_edit_is_flagged_and_reanchors_base_price(seeded_product):
    shop, numeric_id, product_id, numeric_variant_id, variant_id = seeded_product

    result = handle_product_update(shop, _payload(numeric_id, numeric_variant_id, 85.00))

    assert result["ok"] is True
    assert result["manual_edit"] is True
    with get_db() as s:
        variant = s.get(models.ShopifyVariant, variant_id)
        assert float(variant.currentPrice) == 85.00
        assert float(variant.basePrice) == 85.00  # re-anchored


def test_avg_base_price_recomputed(seeded_product):
    shop, numeric_id, product_id, numeric_variant_id, variant_id = seeded_product

    handle_product_update(shop, _payload(numeric_id, numeric_variant_id, 80.00))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert float(product.avgBasePrice) == 80.00


def test_semantic_status_always_reset_but_last_decision_at_only_cleared_on_manual_edit(seeded_product):
    shop, numeric_id, product_id, numeric_variant_id, variant_id = seeded_product
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        product.semanticStatus = "DONE"
        product.lastDecisionAt = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(models.PriceDecision(
            shopDomain=shop, shopifyVariantId=variant_id,
            oldPrice=100.00, newPrice=100.00, reason="no_op", appliedAt=datetime.now(timezone.utc), autoApplied=True,
        ))

    # No price change at all — no manual edit — but semanticStatus still resets.
    handle_product_update(shop, _payload(numeric_id, numeric_variant_id, 100.00, title="Renamed"))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        # semanticStatus is set to PENDING by the product upsert, then
        # immediately claimed to QUEUED by claim_and_enqueue_semantics in
        # the same call — QUEUED is the correct observed end state, proof
        # the embedding pipeline was actually kicked, not just marked dirty.
        assert product.semanticStatus == "QUEUED"
        assert product.lastDecisionAt is not None  # untouched — no manual edit occurred


def test_auto_derived_bounds_recomputed_on_manual_edit(seeded_product):
    shop, numeric_id, product_id, numeric_variant_id, variant_id = seeded_product
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        # Bounds that exactly match the OLD anchor's (100 * (1 ± 0.25)) formula — "auto-derived".
        product.minPriceOverride = 75.00
        product.maxPriceOverride = 125.00

    handle_product_update(shop, _payload(numeric_id, numeric_variant_id, 80.00))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        # New anchor is 80.00 -> bounds should follow: 80*0.75=60.00, 80*1.25=100.00
        assert float(product.minPriceOverride) == 60.00
        assert float(product.maxPriceOverride) == 100.00


def test_merchant_typed_bounds_left_untouched_on_manual_edit(seeded_product):
    shop, numeric_id, product_id, numeric_variant_id, variant_id = seeded_product
    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        # Bounds that do NOT match the old anchor's formula — merchant typed these in by hand.
        product.minPriceOverride = 50.00
        product.maxPriceOverride = 150.00

    handle_product_update(shop, _payload(numeric_id, numeric_variant_id, 80.00))

    with get_db() as s:
        product = s.get(models.ShopifyProduct, product_id)
        assert float(product.minPriceOverride) == 50.00
        assert float(product.maxPriceOverride) == 150.00


def test_manual_edit_on_untracked_variant_writes_no_audit_row(seeded_product):
    shop, numeric_id, product_id, numeric_variant_id, variant_id = seeded_product

    handle_product_update(shop, _payload(numeric_id, numeric_variant_id, 70.00))

    with get_db() as s:
        count = s.query(models.PriceDecision).filter(models.PriceDecision.shopifyVariantId == variant_id).count()
        assert count == 0


def test_manual_edit_on_tracked_variant_writes_audit_row(seeded_product):
    shop, numeric_id, product_id, numeric_variant_id, variant_id = seeded_product
    scraped_id = str(uuid.uuid4())
    scraped_variant_id = str(uuid.uuid4())
    with get_db() as s:
        s.add(models.ScrapedProduct(id=scraped_id, shopDomain=shop, domain="comp.example.com", title="Competitor"))
        s.flush()
        s.add(models.ScrapedVariant(id=scraped_variant_id, productId=scraped_id, title="Default", currentPrice=95.00))
        s.flush()
        s.add(models.ProductMatch(
            id=str(uuid.uuid4()), shopDomain=shop, shopifyVariantId=variant_id,
            competitorVariantId=scraped_variant_id, competitorProdId=scraped_id,
            matchScore=90.0, vectorDistance=0.1, thresholdUsed=0.55,
        ))

    handle_product_update(shop, _payload(numeric_id, numeric_variant_id, 70.00))

    with get_db() as s:
        audit_rows = s.query(models.PriceDecision).filter(models.PriceDecision.shopifyVariantId == variant_id).all()
        assert len(audit_rows) == 1
        assert audit_rows[0].reason == "manual price edit by merchant"
        assert float(audit_rows[0].oldPrice) == 100.00
        assert float(audit_rows[0].newPrice) == 70.00
        s.query(models.ProductMatch).filter(models.ProductMatch.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.ScrapedVariant).filter(models.ScrapedVariant.id == scraped_variant_id).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)


def test_brand_new_product_upserts_successfully_with_updated_at_set(seeded_product):
    """Regression test for the original crash bug: ShopifyProduct.updatedAt
    is a required column with no default; the old JS webhook's create branch
    omitted it and would throw on a genuinely new product."""
    shop, numeric_id, product_id, numeric_variant_id, variant_id = seeded_product
    new_numeric_id = uuid.uuid4().int % 1_000_000_000
    new_product_id = f"gid://shopify/Product/{new_numeric_id}"
    new_numeric_variant_id = uuid.uuid4().int % 1_000_000_000
    new_variant_id = f"gid://shopify/ProductVariant/{new_numeric_variant_id}"

    result = handle_product_update(shop, _payload(new_numeric_id, new_numeric_variant_id, 50.00))

    assert result["ok"] is True
    with get_db() as s:
        product = s.get(models.ShopifyProduct, new_product_id)
        assert product is not None
        assert product.updatedAt is not None
        variant = s.get(models.ShopifyVariant, new_variant_id)
        assert variant is not None
        assert variant.updatedAt is not None
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == new_variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == new_product_id).delete(synchronize_session=False)
