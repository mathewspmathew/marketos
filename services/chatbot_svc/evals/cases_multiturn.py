"""Dev-only multi-turn eval cases for cross-turn grounding regressions.

Separate from cases.py/run.py's 40-case suite on purpose: those run serially
against real Groq calls and take ~10+ minutes, too slow for the tight
iterate-on-the-agent loop this file is for. Run this small set while tuning
prompt/validator/temperature changes (`run_multiturn.py`); once a case is
consistently green, fold its Case(...) into cases.py's build_cases().

Each case is a sequence of real user turns run through the real agent with
real accumulated message history (see runner.run_multiturn_case) -- only the
LAST turn (the probe) is scored. This is the shape the single-turn suite
cannot exercise: a probe answered using facts that leaked in from an earlier,
unrelated turn. Modeled directly on the incident this guards against: turn 1
asked about a real product, turn 2 asked about an unrelated item and the
agent answered with a fabricated product name/price blended from turn 1,
with zero grounding tool call behind it.
"""
from __future__ import annotations

from pydantic_evals import Case

from services.chatbot_svc.evals.cases import shop_price_set
from services.common.db import get_db
from services.common.models import ShopifyProduct, ShopifyVariant


def _products(shop_domain: str, limit: int = 2) -> list[dict]:
    """First `limit` products by title (deterministic), each with its first variant's price."""
    with get_db() as s:
        rows = (
            s.query(ShopifyProduct.title, ShopifyVariant.currentPrice)
            .join(ShopifyVariant, ShopifyVariant.productId == ShopifyProduct.id)
            .filter(ShopifyProduct.shopDomain == shop_domain)
            .order_by(ShopifyProduct.title, ShopifyVariant.id)
            .limit(limit)
            .all()
        )
    seen: dict[str, dict] = {}
    for title, price in rows:
        seen.setdefault(title, {"title": title, "price": float(price)})
    return list(seen.values())


def build_multiturn_cases(shop_domain: str) -> list[Case]:
    products = _products(shop_domain, limit=5)
    if len(products) < 2:
        return []
    allowed = sorted(shop_price_set(shop_domain))
    a, b = products[0], products[1]
    a_short = " ".join(a["title"].split()[:2])
    b_short = " ".join(b["title"].split()[:2])

    return [
        # ── cross-product bleed: turn 2 must re-resolve, not reuse turn 1's fact ──
        Case(
            name="mt_cross_product_no_bleed",
            inputs=[
                f"What is the price of the {a_short}?",
                f"What is the price of the {b_short}?",
            ],
            metadata={
                "expected_facts": [
                    f"This turn only asks for the price of the {b_short} -- the reply should "
                    f"answer that (it does not need to also restate the {a_short}'s price from "
                    f"the earlier turn). The price stated must be for the {b_short} specifically, "
                    f"not the {a_short} or any blend of the two products."
                ],
                "expected_tools": ["resolve_product", "get_variant"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),
        # ── vague follow-up referencing turn 1's product by pronoun/shorthand:
        #    still must call a grounding tool, not answer from memory ──
        Case(
            name="mt_pronoun_followup_still_grounded",
            inputs=[
                f"What is the price of the {a_short}?",
                "and is dynamic pricing on for it?",
            ],
            metadata={
                "expected_facts": [
                    "This turn only asks about dynamic-pricing status (not price) for the "
                    "product named in the earlier turn -- the reply should state whether "
                    "dynamic pricing is on or off for it."
                ],
                "expected_tools": ["get_dynamic_pricing_status"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),
        # ── ambiguous/nonexistent probe after an established product: must
        #    refuse or ask, never invent a product+price out of thin air ──
        Case(
            name="mt_unrelated_nonexistent_probe_no_fabrication",
            inputs=[
                f"What is the price of the {a_short}?",
                "what about the price of the xyzzy widget",
            ],
            metadata={
                "expected_facts": [
                    "This turn asks about a product ('xyzzy widget') that does not exist in "
                    "the store. The reply must NOT state a specific price for it -- it should "
                    "say the product wasn't found or ask for clarification."
                ],
                "expected_tools": ["resolve_product"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),
    ]
