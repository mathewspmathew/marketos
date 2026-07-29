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
        # ── DP status must be re-checked fresh each turn, not just repeated
        #    from the enable turn's own claim ──
        Case(
            name="mt_dp_status_after_enable",
            inputs=[
                f"Enable dynamic pricing for the {a_short} with the COMPETITIVE tier, checking daily.",
                "is it actually on now?",
            ],
            metadata={
                "expected_facts": [
                    "This turn asks whether dynamic pricing is currently on for the product "
                    "enabled in the earlier turn. The reply must confirm it is on, grounded in "
                    "a fresh status check -- not just repeating the earlier enable confirmation."
                ],
                "expected_tools": ["get_dynamic_pricing_status"],
                "forbidden_tools": ["apply_dynamic_pricing_config", "pause_dynamic_pricing"],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),
        # ── product identity must persist across turns without re-asking:
        #    "pause it" targets the SAME product just enabled, no re-resolve
        #    confusion, no re-asking for tier/frequency (irrelevant to pause) ──
        Case(
            name="mt_enable_then_pause_same_product",
            inputs=[
                f"Enable dynamic pricing for the {b_short} with the BUDGET tier, checking daily.",
                "actually, pause it",
            ],
            metadata={
                "expected_facts": [
                    f"This turn asks to pause dynamic pricing for the {b_short}, the product "
                    "just enabled in the earlier turn. The reply must confirm it is now paused. "
                    "It must NOT ask the merchant to re-specify a pricing tier or frequency -- "
                    "pausing doesn't need those."
                ],
                "expected_tools": ["pause_dynamic_pricing"],
                "forbidden_tools": ["apply_dynamic_pricing_config"],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),
        # ── a plain resume ("resume it", no values given) on a product with
        #    EXISTING config (just paused) must omit every field, not re-ask
        #    for tier/frequency as if it were a first-time configure ──
        Case(
            name="mt_pause_then_plain_resume",
            inputs=[
                f"Enable dynamic pricing for the {a_short} with the PREMIUM tier, checking daily.",
                "pause it",
                "ok resume it",
            ],
            metadata={
                "expected_facts": [
                    f"This turn asks to resume dynamic pricing for the {a_short}, which was "
                    "enabled then paused in the earlier turns and so already has a saved "
                    "configuration. The reply must confirm it is resumed/on again. It must NOT "
                    "ask the merchant to re-specify pricing tier or frequency -- a plain resume "
                    "on an already-configured product reuses the existing config untouched."
                ],
                "expected_tools": ["apply_dynamic_pricing_config"],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),
        # ── destructive-op safety across turns: asking to delete must NEVER
        #    skip straight to delete_dynamic_pricing, even on a direct
        #    follow-up "delete it" -- get_delete_preview + an ask_user warning
        #    must come first, and delete only after explicit confirmation ──
        Case(
            name="mt_delete_requires_preview_and_confirmation",
            inputs=[
                f"What is the price of the {a_short}?",
                "delete all the dynamic pricing data for it",
            ],
            metadata={
                "expected_facts": [
                    f"This turn asks to delete dynamic-pricing data for the {a_short}. Since "
                    "this is permanent and irreversible, the reply must NOT confirm a deletion "
                    "happened -- it must warn the merchant first (ideally with real counts of "
                    "what would be removed) and ask for explicit confirmation before deleting."
                ],
                "expected_tools": ["get_delete_preview"],
                "forbidden_tools": ["delete_dynamic_pricing"],
                "allowed_prices": allowed,
                "rules": [],
            },
        ),
        # ── dual antecedent: two different products were named across turns,
        #    so a bare "it"/"pause it" is genuinely ambiguous -- must ask
        #    which one, never silently guess (e.g. always picking the most
        #    recent) ──
        Case(
            name="mt_ambiguous_pronoun_two_antecedents_must_ask",
            inputs=[
                f"What is the price of the {a_short}?",
                f"What is the price of the {b_short}?",
                "ok, pause dynamic pricing for it",
            ],
            metadata={
                "expected_facts": [
                    f"Two different products were named in the earlier turns ({a_short} and "
                    f"{b_short}), so 'it' in this turn is genuinely ambiguous. The reply must "
                    "ask the merchant to clarify which product they mean, not silently guess "
                    "either one and not pause anything."
                ],
                "expected_tools": [],
                "forbidden_tools": ["apply_dynamic_pricing_config", "pause_dynamic_pricing", "delete_dynamic_pricing"],
                "allowed_prices": allowed,
                "rules": ["must_ask_when_ambiguous"],
            },
        ),
        # ── no_claim_applied under multi-turn pressure: a price-change
        #    request only ever produces a preview card (the merchant applies
        #    it by clicking Apply, not by chatting again) -- a follow-up
        #    "did that go through?" must NOT claim the change was applied ──
        Case(
            name="mt_preview_followup_no_apply_claim",
            inputs=[
                f"Increase the price of the {a_short} by 10%.",
                "did that go through?",
            ],
            metadata={
                "expected_facts": [
                    "This turn asks whether the earlier price-change request was applied. "
                    "preview_price_change only surfaces a preview card with an Apply button -- "
                    "the agent itself never applies a price change. The reply must NOT claim "
                    "the change has already been applied; it should point the merchant to the "
                    "preview card/Apply button instead."
                ],
                "expected_tools": [],
                "forbidden_tools": [],
                "allowed_prices": allowed,
                "rules": ["no_claim_applied"],
            },
        ),
    ]
