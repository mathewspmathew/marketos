import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from services.common.db import get_db
from services.common import models
from services.matcher_svc.main import _match_one_pair

_DIM = 768


def _vec(value: float) -> str:
    return "[" + ",".join([str(value)] * _DIM) + "]"


@pytest.fixture
def seeded_pair():
    shop = f"rematch-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    scraped_id = str(uuid.uuid4())
    scraped_variant_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Rematch Test Product",
            vendor="SameBrand", productType="Widget",
        ))
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default", currentPrice=100.00,
        ))
        s.add(models.ScrapedProduct(
            id=scraped_id, shopDomain=shop, domain="comp.example.com",
            title="Competitor Widget", vendor="SameBrand", productType="Widget",
        ))
        s.flush()
        s.add(models.ScrapedVariant(
            id=scraped_variant_id, productId=scraped_id, title="Default",
            currentPrice=95.00, currency="INR",
        ))
        s.flush()
        s.execute(
            text(
                'INSERT INTO "ShopifyEmbedding" (id, "variantId", "shopDomain", "vectorText", "embeddedAt", "updatedAt") '
                'VALUES (:id, :vid, :sd, CAST(:vec AS vector), NOW(), NOW())'
            ),
            {"id": str(uuid.uuid4()), "vid": variant_id, "sd": shop, "vec": _vec(0.1)},
        )
        s.execute(
            text(
                'INSERT INTO "ProductEmbedding" (id, "prodId", "variantId", "shopDomain", "vectorText", "vectorizedAt") '
                'VALUES (:id, :pid, :vid, :sd, CAST(:vec AS vector), NOW())'
            ),
            {"id": str(uuid.uuid4()), "pid": scraped_id, "vid": scraped_variant_id, "sd": shop, "vec": _vec(0.1)},
        )

    yield shop, product_id, variant_id, scraped_id

    with get_db() as s:
        s.execute(text('DELETE FROM "ShopifyEmbedding" WHERE "variantId" = :vid'), {"vid": variant_id})
        s.execute(text('DELETE FROM "ProductEmbedding" WHERE "prodId" = :pid'), {"pid": scraped_id})
        s.query(models.ProductMatch).filter(models.ProductMatch.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.ProductLevelMatch).filter(models.ProductLevelMatch.shopifyProductId == product_id).delete(synchronize_session=False)
        s.query(models.ScrapedVariant).filter(models.ScrapedVariant.id == scraped_variant_id).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_rematch_does_not_reset_merchant_confirmed_review_status(seeded_pair):
    shop, product_id, variant_id, scraped_id = seeded_pair

    with get_db() as s:
        _match_one_pair(s, shop, product_id, scraped_id)

    with get_db() as s:
        match = s.query(models.ProductLevelMatch).filter(
            models.ProductLevelMatch.shopifyProductId == product_id,
            models.ProductLevelMatch.scrapedProductId == scraped_id,
        ).one()
        assert match.reviewStatus == "PENDING"
        match.reviewStatus = "CONFIRMED"

    # Simulate a rescrape re-matching the same pair.
    with get_db() as s:
        _match_one_pair(s, shop, product_id, scraped_id)

    with get_db() as s:
        match = s.query(models.ProductLevelMatch).filter(
            models.ProductLevelMatch.shopifyProductId == product_id,
            models.ProductLevelMatch.scrapedProductId == scraped_id,
        ).one()
        assert match.reviewStatus == "CONFIRMED"
