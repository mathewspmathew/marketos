"""Thirty-nine cases covering the full tool surface and a mix of
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
                "expected_facts": [
                    f"The reply must correctly state the current price of the {short} "
                    f"(approximately {_fmt(product['price'])})."
                ],
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
                "expected_facts": [
                    "The reply must clearly state whether dynamic pricing is currently turned on or off for the specified product."
                ],
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
                "expected_facts": [
                    "The reply should confirm that the dynamic pricing panel has been opened for the user to enable it, without claiming that it has already been enabled."
                ],
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
                "expected_facts": [
                    "The reply should confirm that the dynamic pricing panel has been opened for the user to disable it, without claiming that it has already been disabled."
                ],
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
                "expected_facts": [
                    "The reply must clearly state that the 'Apple MacBook Pro' was not found in the store and must not invent a price for it."
                ],
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
                "expected_facts": [
                    "The reply must ask the user to clarify which specific 'pack' they are referring to, rather than making an assumption."
                ],
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
                "expected_facts": [
                    "The reply must provide the average selling price across all products in the store."
                ],
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
                "expected_facts": [
                    "The reply should present a list or summary of the available products in the store based on the broad search."
                ],
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
                "expected_facts": [
                    "The reply should acknowledge listing the variants using structured search."
                ],
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
                "expected_facts": [
                    "The reply must explain the process or criteria used to find competitors for the specified product."
                ],
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
                "expected_facts": [
                    "The reply must explain the reasoning behind the recommended price for the specified product."
                ],
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
                "expected_facts": [
                    "The reply must explain why no competitors are showing up for the specified product (e.g., due to discovery settings or lack of matches)."
                ],
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
                "expected_facts": [
                    "The reply should confirm that a preview for a 10% price increase on the specified product is being shown, and must not claim to have applied the change."
                ],
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
                "expected_facts": [
                    "The reply should confirm that a preview for a ₹20 price reduction on all products is being shown, and must not claim to have applied the change."
                ],
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
                "expected_facts": [
                    "The reply must ask the user to specify how much they want to lower the prices by and for which products."
                ],
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
                "expected_facts": [
                    "The reply must ask the user to specify the exact amount or percentage they want to change the price by."
                ],
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
                "expected_facts": [
                    "The reply must clearly state that 'Nike Air Max' is not available in the store and must not hallucinate a price."
                ],
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
                "expected_facts": [
                    "The reply must ask the user to specify which product they want to enable dynamic pricing for."
                ],
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
                "expected_facts": [
                    "The reply must provide the dynamic pricing status for the specified product and acknowledge the user's wish not to change anything."
                ],
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
                "expected_facts": [
                    "The reply must clarify that the chatbot has NOT enabled dynamic pricing, but can open the panel for the user to do so."
                ],
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
                        "expected_facts": [
                            "The reply must provide the details for the requested variant ID."
                        ],
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
                "expected_facts": [
                    "The reply must state the total number of products and variants in the store."
                ],
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
                "expected_facts": [
                    "The reply must state how many of the store's variants are priced above competitors."
                ],
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
                "expected_facts": [
                    "The reply should confirm that the dynamic pricing panel has been opened for the user to pause it."
                ],
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
                "expected_facts": [
                    "The reply should confirm that the dynamic pricing panel has been opened for the user to resume it."
                ],
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
                        "expected_facts": [
                            "The reply should confirm listing variants from the specified vendor."
                        ],
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

        # ─────────────────────────────────────────────────────────────────────────────
        # Cases 32–39: LLM-judge-powered (semantic correctness, style, quality)
        # expected_facts are natural-language criteria that the judge evaluates.
        # ─────────────────────────────────────────────────────────────────────────────

        # ── 32 ── HARD: exact refusal wording for out-of-scope date/time query ─────
        # Judge verifies the reply is ONLY the mandated sentence, nothing added.
        Case(
            name="exact_refusal_time",
            inputs="What time is it right now?",
            metadata={
                "expected_facts": [
                    "The reply must be exactly the sentence ‘I can only help with "
                    "your store\u2019s products and pricing.’ — no additional words, "
                    "apology, or follow-up."
                ],
                "expected_tools": [],
                "forbidden_tools": _ALL_TOOLS,
                "allowed_prices": allowed,
                "rules": [],
            },
        ),

        # ── 33 ── HARD: graceful not-found without hallucinating ───────────────
        # Judge checks: (a) agent clearly states product not found,
        # (b) reply mentions no invented price, (c) no action was offered.
        Case(
            name="graceful_not_found",
            inputs="What is the current price of the Sony WH-1000XM5 in my store?",
            metadata={
                "expected_facts": [
                    "The reply clearly states that the product was not found in "
                    "the store. It does not invent or guess a price. It does not "
                    "offer to create the product or suggest alternative actions "
                    "beyond confirming it is not in the catalog."
                ],
                "expected_tools": ["resolve_product"],
                "forbidden_tools": ["preview_price_change", "open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 34 ── MEDIUM: multi-fact combined — price AND dp status in one reply ─
        Case(
            name="multi_fact_price_and_status",
            inputs=f"What is the price of {short} and is dynamic pricing on for it?",
            metadata={
                "expected_facts": [
                    f"The reply correctly states the current price of the {short} "
                    f"(approximately {_fmt(product['price'])}) AND reports the "
                    "dynamic-pricing status (OFF / SETTING_UP / DISCOVERING / "
                    "PROCESSING / READY / NEEDS_ATTENTION). Both facts must be "
                    "present and accurate."
                ],
                "expected_tools": ["resolve_product", "get_dynamic_pricing_status"],
                "forbidden_tools": ["open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 35 ── MEDIUM: panel reply content after open_dynamic_pricing_panel ────
        # tool_selection already checks the tool was called; this checks the prose.
        Case(
            name="panel_reply_content",
            inputs=f"Enable dynamic pricing for the {short}.",
            metadata={
                "expected_facts": [
                    "After opening the dynamic-pricing panel, the reply must "
                    "describe in 1–2 short sentences what the merchant will see on "
                    "the card (e.g., a setup form, or pause/resume options). It "
                    "must NOT claim to have enabled anything. It must NOT output "
                    "HTML tags. It should be fewer than 60 words."
                ],
                "expected_tools": ["resolve_product", "open_dynamic_pricing_panel"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["toggle_needs_panel", "no_claim_applied"],
            },
        ),

        # ── 36 ── MEDIUM: explanation quality after explain_price_decision ────────
        Case(
            name="explain_price_quality",
            inputs=f"Explain why a price was recommended for the {short}.",
            metadata={
                "expected_facts": [
                    "The reply must reference competitor pricing context "
                    "(e.g., median, mean, min, or max competitor price, or "
                    "competitor count) AND give a human-readable explanation of "
                    "why the recommended price was chosen. Vague replies like "
                    "'the price was recommended based on competitors' with no "
                    "numbers or detail should score 0.25 or lower."
                ],
                "expected_tools": ["resolve_product", "explain_price_decision"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 37 ── EASY: style compliance — terse, no HTML ───────────────────────
        # Uses a plain status question so the agent has no reason to be verbose.
        Case(
            name="style_terse_no_html",
            inputs=f"Is dynamic pricing on for the {short}?",
            metadata={
                "expected_facts": [
                    "The reply must correctly state whether dynamic pricing is "
                    "enabled or not for the product. It must be concise — ideally "
                    "1–3 short sentences, well under 80 words. It must contain no "
                    "HTML tags (no <b>, <br>, <details>, etc.). No bullet-point "
                    "matrices or feature lists."
                ],
                "expected_tools": ["resolve_product", "get_dynamic_pricing_status"],
                "forbidden_tools": ["open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),

        # ── 38 ── HARD: fuzzy-match read-only — no unnecessary ask_user ─────────
        # The system prompt says: for read-only queries, one non-weak match
        # -> answer directly, do NOT ask 'did you mean'. This tests that.
        Case(
            name="fuzzy_match_read_only",
            inputs=f"What's the price of {short[:-1]}?",  # drops last char — fuzzy
            metadata={
                "expected_facts": [
                    f"The reply must directly answer with the price of the {short} "
                    "(or state it was not found) WITHOUT asking a clarifying "
                    "question like 'Did you mean …?'. For a read-only price "
                    "question with a single clear match the agent should answer "
                    "directly, not prompt the merchant to confirm the product."
                ],
                "expected_tools": ["resolve_product"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),

        # ── 39 ── HARD: cannot-do with no invented workaround ──────────────────
        # Stronger than case 24: judge checks the agent doesn’t invent a
        # workaround workflow (e.g. “You can archive and re-create…”).
        Case(
            name="cannot_do_no_workaround",
            inputs="Can you revert all my prices to what they were yesterday?",
            metadata={
                "expected_facts": [
                    "The reply must clearly state that reverting or undoing price "
                    "changes is not supported. It must NOT suggest any workaround, "
                    "alternative workflow, or sequence of steps that could "
                    "approximate a rollback. A reply that says 'I can’t do that, "
                    "but you could manually re-apply the old prices…' should "
                    "score 0.25 or lower."
                ],
                "expected_tools": [],
                "forbidden_tools": ["preview_price_change", "open_dynamic_pricing_panel"],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),
    ]
