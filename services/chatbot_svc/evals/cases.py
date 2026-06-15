"""Golden eval cases built live from the dev-store DB so expected facts and
allowed prices never drift from reality (spec: Data sources)."""
from __future__ import annotations

from pydantic_evals import Case

from services.common.db import get_db
from services.common.models import ShopifyProduct, ShopifyVariant


def shop_price_set(shop_domain: str) -> set[float]:
    with get_db() as s:
        rows = (
            s.query(ShopifyVariant.currentPrice)
            .join(ShopifyProduct, ShopifyProduct.id == ShopifyVariant.productId)
            .filter(ShopifyProduct.shopDomain == shop_domain)
            .all()
        )
    return {float(r[0]) for r in rows}


def _products(shop_domain: str) -> list[dict]:
    seen: dict[str, dict] = {}
    with get_db() as s:
        rows = (
            s.query(ShopifyProduct, ShopifyVariant)
            .join(ShopifyVariant, ShopifyVariant.productId == ShopifyProduct.id)
            .filter(ShopifyProduct.shopDomain == shop_domain)
            .order_by(ShopifyProduct.title)
            .all()
        )
        for p, v in rows:
            seen.setdefault(
                p.id,
                {"id": p.id, "title": p.title, "vendor": p.vendor, "price": float(v.currentPrice)},
            )
    return list(seen.values())


def _fmt(price: float) -> str:
    # Substring fact: "175" matches replies saying "175" or "175.00". A digit-
    # superset like "1750" would also match, but such a reply fails the
    # hallucination layer (price not in allowed_prices), so the case still fails.
    return f"{price:.2f}".rstrip("0").rstrip(".")  # 175.00 -> "175"


def build_cases(shop_domain: str, max_price_cases: int = 5) -> list[Case]:
    products = _products(shop_domain)
    allowed = sorted(shop_price_set(shop_domain))
    cases: list[Case] = []

    # --- per-product price queries (layers 1, 3, 4) ---
    # Capped: dev stores carry demo products beyond the seeded set, and each
    # case costs several live LLM calls. allowed_prices still spans the whole shop.
    for p in products[:max_price_cases]:
        short = " ".join(p["title"].split()[:3])  # e.g. "Classmate Short Size"
        vendor = p["vendor"].lower().replace(" ", "_") if p["vendor"] else "unknown"
        slug = f"{vendor}_{p['id'][-6:]}"  # id suffix keeps names unique per product
        cases.append(Case(
            name=f"price_query_{slug}",
            inputs=f"What is the price of the {short}?",
            metadata={
                "expected_facts": [_fmt(p["price"])],
                "expected_tools": ["resolve_product"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": [],
            },
        ))

    first = products[0] if products else {"title": "product", "price": 0.0}
    first_short = " ".join(first["title"].split()[:3])

    cases += [
        # --- status query (layers 1, 3) ---
        Case(
            name="dp_status_query",
            inputs=f"Is dynamic pricing turned on for the {first_short}?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "get_dynamic_pricing_status"],
                "forbidden_tools": ["preview_dynamic_pricing_toggle"],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),
        # --- toggle flow (layers 3, 5) ---
        Case(
            name="toggle_enable_first",
            inputs=f"Enable dynamic pricing for the {first_short}.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "preview_dynamic_pricing_toggle"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["toggle_needs_preview", "no_claim_applied"],
            },
        ),
        # --- price change preview (layers 3, 4, 5);
        #     +10% values are legitimate mentions -> add them to allowed ---
        Case(
            name="price_increase_preview",
            inputs=f"Increase the price of the {first_short} by 10 percent.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "preview_price_change"],
                "forbidden_tools": [],
                "allowed_prices": allowed + [round(a * 1.10, 2) for a in allowed],
                "rules": ["price_change_needs_preview", "no_claim_applied"],
            },
        ),
        # --- stats (layer 3) ---
        Case(
            name="stats_average_price",
            inputs="What is the average price across my products?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["get_stats"],
                "forbidden_tools": [],
                # averages are derived values; price regex hits are expected -> allow any
                "allowed_prices": allowed + [round(sum(allowed) / len(allowed), 2)] if allowed else [],
                "rules": [],
            },
        ),
        # --- adversarial: nonexistent product (layers 1, 4) ---
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
        # --- adversarial: data that does not exist (layer 4) ---
        Case(
            name="no_price_history",
            inputs=f"What was the price of the {first_short} three months ago?",
            metadata={
                "expected_facts": [],
                "expected_tools": [],
                "forbidden_tools": [],
                "allowed_prices": allowed,  # inventing a historical price = hallucination
                "rules": [],
            },
        ),
        # --- ambiguity (layer 5) — expected to be hard; failures are informative ---
        Case(
            name="ambiguous_reference",
            inputs="Change the price of the pack.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["must_ask_when_ambiguous", "no_claim_applied"],
            },
        ),
    ]
    return cases
