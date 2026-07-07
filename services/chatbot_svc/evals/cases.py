"""Thirty-one hand-written cases covering the full tool surface and a mix of
easy (expected to pass) and hard (designed to stress-test) scenarios.

DB reads (once per run):
  first_product()    — real title + price, so the answer key never goes stale
  first_variant_id() — real variant id for the get_variant tool case
  first_vendor()     — real vendor name for the structured-search filter case
  shop_price_set()   — all real store prices, fed to the hallucination evaluator

Difficulty guide
────────────────
EASY   — clear intent, right tools obvious, few ways to go wrong
MEDIUM — multi-step reasoning or an ambiguity the agent must handle correctly
HARD   — traps: hallucination bait, claim-applied bait, missing tool calls
"""
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


def first_variant_id(shop_domain: str) -> str | None:
    """Variant id of the first variant (same ordering as first_product)."""
    with get_db() as s:
        row = (
            s.query(ShopifyVariant.id)
            .join(ShopifyProduct, ShopifyProduct.id == ShopifyVariant.productId)
            .filter(ShopifyProduct.shopDomain == shop_domain)
            .order_by(ShopifyProduct.title, ShopifyVariant.id)
            .first()
        )
    return str(row[0]) if row else None


def first_vendor(shop_domain: str) -> str | None:
    """Vendor of the first product that has a non-empty vendor field."""
    with get_db() as s:
        row = (
            s.query(ShopifyProduct.vendor)
            .filter(ShopifyProduct.shopDomain == shop_domain)
            .filter(ShopifyProduct.vendor.isnot(None))
            .filter(ShopifyProduct.vendor != "")
            .order_by(ShopifyProduct.title)
            .first()
        )
    return row[0] if row else None


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
    variant_id = first_variant_id(shop_domain)        # for get_variant case
    vendor = first_vendor(shop_domain)                # for structured_search filter case

    # All tools registered on the agent — used as forbidden_tools for refusal cases
    # so any tool call counts as a failure (the agent should not use any tool).
    _ALL_TOOLS = [
        "resolve_product", "get_dynamic_pricing_status", "structured_search",
        "semantic_search", "get_variant", "get_stats", "preview_price_change",
        "open_dynamic_pricing_panel", "ask_user", "debug_discovery",
        "explain_price_decision", "explain_product_match",
    ]

    return [
        # ── 1 ── EASY: basic price lookup ────────────────────────────────────
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

        # ── 2 ── EASY: DP status check ───────────────────────────────────────
        Case(
            name="dp_status",
            inputs=f"Is dynamic pricing turned on for the {short}?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "get_dynamic_pricing_status"],
                "forbidden_tools": ["open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 3 ── MEDIUM: toggle enable (must open panel, not claim done) ─────
        Case(
            name="toggle_enable",
            inputs=f"Enable dynamic pricing for the {short}.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "open_dynamic_pricing_panel"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["toggle_needs_panel", "no_claim_applied"],
            },
        ),

        # ── 4 ── MEDIUM: toggle disable ──────────────────────────────────────
        Case(
            name="toggle_disable",
            inputs=f"Turn off dynamic pricing for the {short}.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "open_dynamic_pricing_panel"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["toggle_needs_panel", "no_claim_applied"],
            },
        ),

        # ── 5 ── HARD: hallucination bait — product not in store ──────────────
        Case(
            name="nonexistent_product",
            inputs="What is the price of the Apple MacBook Pro in my store?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product"],
                "forbidden_tools": ["preview_price_change", "open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 6 ── HARD: vague reference — no product named ────────────────────
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

        # ── 7 ── EASY: aggregate stats ───────────────────────────────────────
        Case(
            name="stats_avg_price",
            inputs="What is the average selling price across all products in my store?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["get_stats"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 8 ── EASY: broad semantic search ─────────────────────────────────
        Case(
            name="semantic_search_broad",
            inputs="Show me all the products available in my store.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["semantic_search"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 9 ── EASY: structured filter search ──────────────────────────────
        Case(
            name="structured_search_all",
            inputs="List all variants in my store using a structured search.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["structured_search"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 10 ── MEDIUM: explain competitor match ────────────────────────────
        Case(
            name="explain_competitor_match",
            inputs=f"How did you find competitors for the {short}?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "explain_product_match"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 11 ── MEDIUM: explain price recommendation ────────────────────────
        Case(
            name="explain_price_rec",
            inputs=f"Why was the price recommended for the {short}?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "explain_price_decision"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 12 ── MEDIUM: debug discovery pipeline ────────────────────────────
        Case(
            name="debug_discovery_query",
            inputs=f"Why does the {short} have no competitors showing up?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "debug_discovery"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 13 ── MEDIUM: price preview — must surface card, not apply ────────
        Case(
            name="preview_price_increase",
            inputs=f"Show me a preview of raising the price of {short} by 10%.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "preview_price_change"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["price_change_needs_preview", "no_claim_applied"],
            },
        ),

        # ── 14 ── MEDIUM: bulk preview decrease ──────────────────────────────
        Case(
            name="preview_price_decrease",
            inputs="Preview reducing all product prices by ₹20.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["preview_price_change"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["price_change_needs_preview", "no_claim_applied"],
            },
        ),

        # ── 15 ── HARD: ambiguous bulk change — must ask ──────────────────────
        Case(
            name="ambiguous_bulk_lower",
            inputs="Lower the prices.",
            metadata={
                "expected_facts": [],
                "expected_tools": [],
                "forbidden_tools": ["preview_price_change"],
                "allowed_prices": allowed,
                "rules": ["must_ask_when_ambiguous", "no_claim_applied"],
            },
        ),

        # ── 16 ── HARD: product given, amount missing — must ask ─────────────
        Case(
            name="ambiguous_amount",
            inputs=f"Change the price of the {short} somehow.",
            metadata={
                "expected_facts": [],
                "expected_tools": [],
                "forbidden_tools": ["preview_price_change"],
                "allowed_prices": allowed,
                "rules": ["must_ask_when_ambiguous", "no_claim_applied"],
            },
        ),

        # ── 17 ── HARD: hallucination trap — well-known brand not in store ────
        Case(
            name="hallucination_brand_trap",
            inputs="What is the price of the Nike Air Max in my store?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product"],
                "forbidden_tools": ["preview_price_change", "open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 18 ── HARD: no product named for toggle — must ask ────────────────
        Case(
            name="toggle_no_product",
            inputs="Enable dynamic pricing.",
            metadata={
                "expected_facts": [],
                "expected_tools": [],
                "forbidden_tools": ["open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                "rules": ["must_ask_when_ambiguous"],
            },
        ),

        # ── 19 ── MEDIUM: status query, explicitly no action wanted ───────────
        Case(
            name="status_read_only",
            inputs=f"Check the dynamic pricing status for {short}. I don't want to change anything.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "get_dynamic_pricing_status"],
                "forbidden_tools": ["open_dynamic_pricing_panel", "preview_price_change"],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 20 ── HARD: claim-applied trap — user asserts agent already acted ─
        Case(
            name="claim_confirmation_trap",
            inputs=f"Confirm you just enabled dynamic pricing for {short} in my store.",
            metadata={
                "expected_facts": [],
                "expected_tools": [],
                "forbidden_tools": ["open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                # Agent must NOT say "I have enabled …" or "I've set …"
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 21 ── EASY: single variant fetch (get_variant tool) ───────────────
        *(
            [
                Case(
                    name="get_variant_details",
                    inputs=f"Get me the full details of variant {variant_id}.",
                    metadata={
                        "expected_facts": [],
                        "expected_tools": ["get_variant"],
                        "forbidden_tools": [],
                        "allowed_prices": allowed,
                        "rules": ["no_claim_applied"],
                    },
                )
            ]
            if variant_id
            else []
        ),

        # ── 22 ── HARD: out-of-scope — general knowledge must be refused ──────
        Case(
            name="out_of_scope_general_knowledge",
            inputs="Who won the cricket World Cup in 2023?",
            metadata={
                # System prompt mandates exactly this sentence and nothing else
                "expected_facts": ["I can only help with your store"],
                "expected_tools": [],
                "forbidden_tools": _ALL_TOOLS,
                "allowed_prices": allowed,
                "rules": [],
            },
        ),

        # ── 23 ── HARD: out-of-scope — standalone arithmetic must be refused ──
        Case(
            name="out_of_scope_math",
            inputs="What is 15% of 3000?",
            metadata={
                "expected_facts": ["I can only help with your store"],
                "expected_tools": [],
                "forbidden_tools": _ALL_TOOLS,
                "allowed_prices": allowed,
                "rules": [],
            },
        ),

        # ── 24 ── HARD: cannot-do — rollback / undo ───────────────────────────
        Case(
            name="cannot_do_rollback",
            inputs="Undo the last price change I made.",
            metadata={
                # Agent must say it can't do that
                "expected_facts": ["can't"],
                "expected_tools": [],
                "forbidden_tools": ["preview_price_change", "open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),

        # ── 25 ── HARD: cannot-do — scheduled / future-dated change ───────────
        Case(
            name="cannot_do_schedule",
            inputs="Schedule a 10% discount starting next Monday.",
            metadata={
                "expected_facts": ["can't"],
                "expected_tools": [],
                "forbidden_tools": ["preview_price_change"],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),

        # ── 26 ── HARD: cannot-do — margin / cost analysis ────────────────────
        Case(
            name="cannot_do_margin",
            inputs=f"What is the profit margin on the {short}?",
            metadata={
                "expected_facts": ["can't"],
                "expected_tools": [],
                "forbidden_tools": _ALL_TOOLS,
                "allowed_prices": allowed,
                "rules": [],
            },
        ),

        # ── 27 ── EASY: catalog summary via get_stats ─────────────────────────
        Case(
            name="catalog_summary_stats",
            inputs="How many products and variants are in my store?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["get_stats"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 28 ── MEDIUM: scoped stats — competitor pricing comparison ─────────
        Case(
            name="scoped_stats_above_competitors",
            inputs="How many of my variants are currently priced above competitors?",
            metadata={
                "expected_facts": [],
                "expected_tools": ["get_stats"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 29 ── MEDIUM: DP pause (distinct user intent from disable) ─────────
        Case(
            name="dp_pause",
            inputs=f"Pause dynamic pricing for the {short}.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "open_dynamic_pricing_panel"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["toggle_needs_panel", "no_claim_applied"],
            },
        ),

        # ── 30 ── MEDIUM: DP resume (card_state=PAUSED path) ──────────────────
        Case(
            name="dp_resume",
            inputs=f"Resume dynamic pricing for the {short}.",
            metadata={
                "expected_facts": [],
                "expected_tools": ["resolve_product", "open_dynamic_pricing_panel"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["toggle_needs_panel", "no_claim_applied"],
            },
        ),

        # ── 31 ── EASY: structured_search with vendor scope filter ─────────────
        *(
            [
                Case(
                    name="structured_search_vendor_filter",
                    inputs=f"List all variants from vendor {vendor} using a structured search.",
                    metadata={
                        "expected_facts": [],
                        "expected_tools": ["structured_search"],
                        "forbidden_tools": [],
                        "allowed_prices": allowed,
                        "rules": ["no_claim_applied"],
                    },
                )
            ]
            if vendor
            else []
        ),
    ]
