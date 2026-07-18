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
def chat_session_id(seed_shop):
    """A ChatSession for seed_shop with its product already recorded as
    resolved — lets existing happy-path tests call the 3 mutation functions
    without separately exercising the new resolution guard."""
    from datetime import datetime, timezone

    from services.common.models import ChatSession, ShopifyProduct

    session_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        product_id = s.query(ShopifyProduct.id).filter(ShopifyProduct.shopDomain == seed_shop).scalar()
        s.add(ChatSession(
            id=session_id, shopDomain=seed_shop, resolvedProductIds=[product_id],
            createdAt=now, updatedAt=now,
        ))

    yield session_id

    with get_db() as s:
        s.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)


@pytest.fixture
def seed_other_shop():
    """A completely separate shop so we can assert cross-shop isolation."""
    shop = f"other-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = "other-shop-product"
    variant_id = "other-shop-variant"

    with get_db() as s:
        # Defensive: these are fixed PKs, so a crashed prior teardown could leave
        # them behind and duplicate-key on insert. Clear any stragglers first
        # (FK-safe order: variant before product).
        s.query(ShopifyVariant).filter(ShopifyVariant.id == variant_id).delete(
            synchronize_session=False
        )
        s.query(ShopifyProduct).filter(ShopifyProduct.id == product_id).delete(
            synchronize_session=False
        )
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
