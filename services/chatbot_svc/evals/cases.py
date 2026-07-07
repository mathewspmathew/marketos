"""Five hand-written golden cases. Only the first product's title and price
are read live from the dev-store DB so the answer key never goes stale."""
from __future__ import annotations

from pydantic_evals import Case

from services.common.db import get_db
from services.common.models import ShopifyProduct, ShopifyVariant

# shop_price_set() (all real prices in your store, used to catch hallucinated prices).
def shop_price_set(shop_domain: str) -> set[float]:
    with get_db() as s:
        rows = (
            s.query(ShopifyVariant.currentPrice)
            .join(ShopifyProduct, ShopifyProduct.id == ShopifyVariant.productId)
            .filter(ShopifyProduct.shopDomain == shop_domain)
            .all()
        )
    return {float(r[0]) for r in rows}

# The only DB reads are first_product() (to get a real product title + price )
def first_product(shop_domain: str) -> dict | None:
    """First product by title (deterministic) with its first variant's price."""
    with get_db() as s:
        row = (
            s.query(ShopifyProduct, ShopifyVariant)
            .join(ShopifyVariant, ShopifyVariant.productId == ShopifyProduct.id)
            .filter(ShopifyProduct.shopDomain == shop_domain)
            .order_by(ShopifyProduct.title, ShopifyVariant.id)
            .first()
        )
        if row is None:
            return None
        product, variant = row
        return {"title": product.title, "price": float(variant.currentPrice)}


def _fmt(price: float) -> str:
    # Substring fact: "175" matches replies saying "175" or "175.00". A digit-
    # superset like "1750" would also match, but such a reply fails the
    # hallucination layer (price not in allowed_prices), so the case still fails.
    return f"{price:.2f}".rstrip("0").rstrip(".")  # 175.00 -> "175"


def build_cases(shop_domain: str) -> list[Case]:
    product = first_product(shop_domain)
    if product is None:
        return []
    allowed = sorted(shop_price_set(shop_domain))
    short = " ".join(product["title"].split()[:3])  # e.g. "Boat Speaker White"

    return [
        Case(
            name="price_query",
            inputs=f"What is the price of the {short}?",
            metadata={
                "expected_facts": [_fmt(product["price"])],
                "expected_tools": ["resolve_product"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),
        Case(
            name="dp_status",
            inputs=f"Is dynamic pricing turned on for the {short}?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "get_dynamic_pricing_status"],
                "forbidden_tools": ["preview_dynamic_pricing_toggle"],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),
        Case(
            name="toggle_enable",
            inputs=f"Enable dynamic pricing for the {short}.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "preview_dynamic_pricing_toggle"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["toggle_needs_preview", "no_claim_applied"],
            },
        ),
        Case(
            name="nonexistent_product",
            inputs="What is the price of the Apple MacBook Pro in my store?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product"],
                "forbidden_tools": ["preview_price_change", "preview_dynamic_pricing_toggle"],
                "allowed_prices": allowed,  # any price not in the store = hallucination
                "rules": ["no_claim_applied"],
            },
        ),
        Case(
            name="ambiguous_reference",
            inputs="Change the price of the pack.",
            metadata={
                "expected_facts": [],
                "expected_tools": [],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),
    ]
