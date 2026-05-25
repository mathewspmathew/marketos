import uuid

import pytest

from services.common.db import get_db
from services.common.models import ShopifyProduct, ShopifyUser, ShopifyVariant


@pytest.fixture
def seed_shop():
    """Create one shop with one Boat product and one white variant; tear down after."""
    shop = f"test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"

    with get_db() as s:
        s.add(ShopifyUser(shopDomain=shop))
        s.flush()

        p = ShopifyProduct(
            id=product_id,
            shopDomain=shop,
            title="Boat Speaker White",
            vendor="Boat",
            productType="audio",
            tags=["audio", "white"],
            dynamicPricingEnabled=False,
        )
        s.add(p)
        s.flush()

        v = ShopifyVariant(
            id=variant_id,
            productId=product_id,
            title="white",
            currentPrice=99.0,
            options={"color": "white"},
        )
        s.add(v)

    yield shop

    # Teardown: delete in FK-safe order
    with get_db() as s:
        s.query(ShopifyVariant).filter(
            ShopifyVariant.productId.in_(
                s.query(ShopifyProduct.id).filter(ShopifyProduct.shopDomain == shop)
            )
        ).delete(synchronize_session=False)
        s.query(ShopifyProduct).filter(ShopifyProduct.shopDomain == shop).delete(
            synchronize_session=False
        )
        s.query(ShopifyUser).filter(ShopifyUser.shopDomain == shop).delete(
            synchronize_session=False
        )


@pytest.fixture
def seed_other_shop():
    """A completely separate shop so we can assert cross-shop isolation."""
    shop = f"other-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = "other-shop-product"
    variant_id = "other-shop-variant"

    with get_db() as s:
        existing = s.get(ShopifyUser, shop)
        if not existing:
            s.add(ShopifyUser(shopDomain=shop))
            s.flush()

            p = ShopifyProduct(
                id=product_id,
                shopDomain=shop,
                title="X",
                vendor="X",
                productType="x",
                tags=[],
                dynamicPricingEnabled=False,
            )
            s.add(p)
            s.flush()

            v = ShopifyVariant(
                id=variant_id,
                productId=product_id,
                title="x",
                currentPrice=1.0,
                options={},
            )
            s.add(v)

    yield shop

    # Teardown
    with get_db() as s:
        s.query(ShopifyVariant).filter(ShopifyVariant.id == variant_id).delete(
            synchronize_session=False
        )
        s.query(ShopifyProduct).filter(ShopifyProduct.id == product_id).delete(
            synchronize_session=False
        )
        s.query(ShopifyUser).filter(ShopifyUser.shopDomain == shop).delete(
            synchronize_session=False
        )
