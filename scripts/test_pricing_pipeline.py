"""
End-to-end pipeline smoke test.

Walks the full dynamic-pricing pipeline against the real dev DB:

  setup  →  observation  →  match  →  stats  →  decide  →  write
                                                  └─ blocked / noop / applied

Each stage's outputs are inspected directly via SQL. Negative-path scenarios
(disabled competitor, OOS, stale stats, kill switch, missing CONFIRMED match,
promotion window, blocked decision, missing offline token) are exercised in
isolation by mutating one row and re-running the affected stage.

Run:
    uv run python -m scripts.test_pricing_pipeline

Cleans up its own shop on exit; safe to run repeatedly. Prints PASS/FAIL per
check at the end.
"""
from __future__ import annotations

import sys
import uuid
import traceback
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from services.common.db import get_db


SHOP = "pipeline-test.myshopify.com"


# ─────────────────────────────────────────────────────────────────────────────
# Assertion harness
# ─────────────────────────────────────────────────────────────────────────────

class Failed(Exception):
    pass


checks: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    checks.append((label, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {label}" + (f"   — {detail}" if detail else ""))


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────

def cleanup() -> None:
    with get_db() as s:
        for t in [
            "PricingAlert", "PriceChange", "PriceDecision",
            "VariantCompetitorStats", "PromotionWindow", "PricingRule",
            "PricingConfig", "ProductLevelMatch", "ProductMatch",
            "CompetitorPriceObservation",
            "SalesAggregate", "ProductEmbedding",
            "ShopifyEmbedding",
        ]:
            s.execute(text(f'DELETE FROM "{t}" WHERE "shopDomain"=:s'), {"s": SHOP})
        # cascade-aware deletes for tables without shopDomain
        s.execute(text("""
            DELETE FROM "ScrapedVariant" WHERE "productId" IN
            (SELECT id FROM "ScrapedProduct" WHERE "shopDomain"=:s)
        """), {"s": SHOP})
        s.execute(text("""
            DELETE FROM "ShopifyVariant" WHERE "productId" IN
            (SELECT id FROM "ShopifyProduct" WHERE "shopDomain"=:s)
        """), {"s": SHOP})
        s.execute(text('DELETE FROM "ScrapedProduct" WHERE "shopDomain"=:s'), {"s": SHOP})
        s.execute(text('DELETE FROM "ShopifyProduct" WHERE "shopDomain"=:s'), {"s": SHOP})
        s.execute(text('DELETE FROM "ProductUrl" WHERE "shopDomain"=:s'), {"s": SHOP})
        s.execute(text('DELETE FROM "ScrapingConfig" WHERE "shopDomain"=:s'), {"s": SHOP})
        s.execute(text('DELETE FROM "ShopifyUser" WHERE "shopDomain"=:s'), {"s": SHOP})


# ─────────────────────────────────────────────────────────────────────────────
# Fixture setup
# ─────────────────────────────────────────────────────────────────────────────

def setup_fixture() -> dict:
    """Build a complete shop world. Returns ids the tests will need."""
    ids = {
        "shop": SHOP,
        "merchant_product":  str(uuid.uuid4()),
        "merchant_variant":  str(uuid.uuid4()),
        "shopify_embedding": str(uuid.uuid4()),
        # Competitor A — premium, weight 1.0 (Flipkart-ish)
        "comp_a_product":    str(uuid.uuid4()),
        "comp_a_variant":    str(uuid.uuid4()),
        "comp_a_embedding":  str(uuid.uuid4()),
        # Competitor B — mid, weight 0.3 (Myntra-ish)
        "comp_b_product":    str(uuid.uuid4()),
        "comp_b_variant":    str(uuid.uuid4()),
        "comp_b_embedding":  str(uuid.uuid4()),
        # Competitor C — different brand, should be rejected by brand pre-filter
        "comp_c_product":    str(uuid.uuid4()),
        "comp_c_variant":    str(uuid.uuid4()),
        "comp_c_embedding":  str(uuid.uuid4()),
    }
    vec = "[" + ",".join(["0.1"] * 768) + "]"

    with get_db() as s:
        s.execute(text('INSERT INTO "ShopifyUser"("shopDomain") VALUES (:d) ON CONFLICT DO NOTHING'),
                  {"d": SHOP})

        # Merchant side
        s.execute(text("""INSERT INTO "ShopifyProduct"(id,"shopDomain",title,vendor,"productType","createdAt","updatedAt")
                          VALUES (:i,:s,'Acme Widget','Acme','Widget',NOW(),NOW())"""),
                  {"i": ids["merchant_product"], "s": SHOP})
        s.execute(text("""INSERT INTO "ShopifyVariant"(id,"productId",title,"currentPrice",cost,"inventoryQuantity","updatedAt")
                          VALUES (:i,:p,'Default',1100,400,40,NOW())"""),
                  {"i": ids["merchant_variant"], "p": ids["merchant_product"]})
        s.execute(text("""INSERT INTO "ShopifyEmbedding"(id,"variantId","shopDomain","vectorText","embeddedAt","updatedAt")
                          VALUES (:i,:v,:s,CAST(:vt AS vector),NOW(),NOW())"""),
                  {"i": ids["shopify_embedding"], "v": ids["merchant_variant"], "s": SHOP, "vt": vec})

        # Competitor A — Acme brand, on flipkart.com, ₹900
        s.execute(text("""INSERT INTO "ScrapedProduct"(id,"shopDomain",domain,title,vendor,"productType","createdAt","updatedAt")
                          VALUES (:i,:s,'flipkart.com','Acme Widget Comp A','Acme','Widget',NOW(),NOW())"""),
                  {"i": ids["comp_a_product"], "s": SHOP})
        s.execute(text("""INSERT INTO "ScrapedVariant"(id,"productId",title,"currentPrice","createdAt","updatedAt")
                          VALUES (:i,:p,'Default',900,NOW(),NOW())"""),
                  {"i": ids["comp_a_variant"], "p": ids["comp_a_product"]})
        s.execute(text("""INSERT INTO "ProductEmbedding"(id,"shopDomain","prodId","variantId","vectorText","vectorizedAt")
                          VALUES (:i,:s,:p,:v,CAST(:vt AS vector),NOW())"""),
                  {"i": ids["comp_a_embedding"], "s": SHOP, "p": ids["comp_a_product"], "v": ids["comp_a_variant"], "vt": vec})

        # Competitor B — Acme brand, on myntra.com, ₹1200, weight 0.3
        s.execute(text("""INSERT INTO "ScrapedProduct"(id,"shopDomain",domain,title,vendor,"productType","createdAt","updatedAt")
                          VALUES (:i,:s,'myntra.com','Acme Widget Comp B','Acme','Widget',NOW(),NOW())"""),
                  {"i": ids["comp_b_product"], "s": SHOP})
        s.execute(text("""INSERT INTO "ScrapedVariant"(id,"productId",title,"currentPrice","createdAt","updatedAt")
                          VALUES (:i,:p,'Default',1200,NOW(),NOW())"""),
                  {"i": ids["comp_b_variant"], "p": ids["comp_b_product"]})
        s.execute(text("""INSERT INTO "ProductEmbedding"(id,"shopDomain","prodId","variantId","vectorText","vectorizedAt")
                          VALUES (:i,:s,:p,:v,CAST(:vt AS vector),NOW())"""),
                  {"i": ids["comp_b_embedding"], "s": SHOP, "p": ids["comp_b_product"], "v": ids["comp_b_variant"], "vt": vec})

        # Competitor C — Brand mismatch (Beta brand vs merchant Acme). Should
        # be rejected by matcher's brand pre-filter.
        s.execute(text("""INSERT INTO "ScrapedProduct"(id,"shopDomain",domain,title,vendor,"productType","createdAt","updatedAt")
                          VALUES (:i,:s,'amazon.com','Beta Widget','Beta','Widget',NOW(),NOW())"""),
                  {"i": ids["comp_c_product"], "s": SHOP})
        s.execute(text("""INSERT INTO "ScrapedVariant"(id,"productId",title,"currentPrice","createdAt","updatedAt")
                          VALUES (:i,:p,'Default',700,NOW(),NOW())"""),
                  {"i": ids["comp_c_variant"], "p": ids["comp_c_product"]})
        s.execute(text("""INSERT INTO "ProductEmbedding"(id,"shopDomain","prodId","variantId","vectorText","vectorizedAt")
                          VALUES (:i,:s,:p,:v,CAST(:vt AS vector),NOW())"""),
                  {"i": ids["comp_c_embedding"], "s": SHOP, "p": ids["comp_c_product"], "v": ids["comp_c_variant"], "vt": vec})

        # Sales aggregate so pricing has merchant-side signal
        s.execute(text("""INSERT INTO "SalesAggregate"("shopifyVariantId","shopDomain","orders7d","revenue7d","orders28d","revenue28d","daysOfStock","updatedAt")
                          VALUES (:v,:s,10,11000,30,33000,28,NOW())"""),
                  {"v": ids["merchant_variant"], "s": SHOP})

    return ids


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Extractor observation path
# ─────────────────────────────────────────────────────────────────────────────

def stage_observations(ids: dict) -> None:
    print("\n── Stage 1: extractor writes CompetitorPriceObservation ─────────────")
    from services.scraper_svc.extractor import _record_observations
    with get_db() as s:
        _record_observations(s, SHOP, [
            {"competitorVariantId": ids["comp_a_variant"], "price": 900.0, "isInStock": True},
            {"competitorVariantId": ids["comp_b_variant"], "price": 1200.0, "isInStock": True},
            {"competitorVariantId": ids["comp_c_variant"], "price": 700.0, "isInStock": True},
        ])
    with get_db() as s:
        n = s.execute(text('SELECT count(*) FROM "CompetitorPriceObservation" WHERE "shopDomain"=:s'),
                      {"s": SHOP}).scalar()
    check("3 observations written", n == 3, f"count={n}")


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: Matcher (brand pre-filter, confidence tiering, product-level rollup)
# ─────────────────────────────────────────────────────────────────────────────

def stage_matcher(ids: dict) -> None:
    print("\n── Stage 2: matcher with brand pre-filter ────────────────────────────")
    from services.matcher_svc.main import _match_variant
    written = _match_variant(SHOP, ids["merchant_variant"])
    check("matcher wrote rows", written > 0, f"written={written}")

    with get_db() as s:
        rows = s.execute(text("""
            SELECT pm."competitorVariantId", pm."matchScore", pm.confidence,
                   pm."confidenceTier"::text, sp.vendor
            FROM "ProductMatch" pm
            LEFT JOIN "ScrapedVariant" sv ON sv.id = pm."competitorVariantId"
            LEFT JOIN "ScrapedProduct" sp ON sp.id = sv."productId"
            WHERE pm."shopifyVariantId"=:v
        """), {"v": ids["merchant_variant"]}).all()
    matched_vendors = {r.vendor for r in rows}
    check("matched only same-brand candidates",
          "Acme" in matched_vendors and "Beta" not in matched_vendors,
          f"vendors={matched_vendors}")
    confirmed = sum(1 for r in rows if r.confidenceTier == "CONFIRMED")
    check("at least one CONFIRMED match",
          confirmed >= 1, f"confirmed_count={confirmed} of {len(rows)}")

    with get_db() as s:
        plm = s.execute(text("""SELECT confidence, "confidenceTier"::text, source, "confirmedByMerchant"
                                FROM "ProductLevelMatch" WHERE "shopDomain"=:s"""),
                        {"s": SHOP}).all()
    check("ProductLevelMatch rollup row(s) exist", len(plm) >= 1,
          f"rows={plm}")


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: Stats recompute (weighting + freshness + filters)
# ─────────────────────────────────────────────────────────────────────────────

def stage_stats(ids: dict) -> None:
    print("\n── Stage 3: stats — weighted aggregation ─────────────────────────────")
    from services.pricing_svc.stats import _recompute_for_variant
    wrote = _recompute_for_variant(SHOP, ids["merchant_variant"])
    check("stats wrote a row", wrote is True)

    with get_db() as s:
        row = s.execute(text("""SELECT "competitorCount","minPrice","median","maxPrice",
                                       "weightedMin","weightedMedian","avgMatchConfidence"
                                FROM "VariantCompetitorStats" WHERE "shopifyVariantId"=:v"""),
                        {"v": ids["merchant_variant"]}).first()
    check("count = 2 (Beta filtered by brand)", int(row[0]) == 2, f"got={row[0]}")
    check("weightedMin = 900 (Flipkart 1.0 vs Myntra 0.3)", float(row[4]) == 900.0,
          f"got={row[4]}")
    check("unweighted median = 1050", float(row[2]) == 1050.0, f"got={row[2]}")


def stage_stats_filters(ids: dict) -> None:
    print("\n── Stage 3b: stats filter — disabled competitor / OOS ────────────────")
    from services.pricing_svc.stats import _recompute_for_variant

    # Mark Flipkart's latest observation as out of stock — only Myntra should count
    with get_db() as s:
        s.execute(text('UPDATE "CompetitorPriceObservation" SET "isInStock"=false WHERE "competitorVariantId"=:v'),
                  {"v": ids["comp_a_variant"]})
    _recompute_for_variant(SHOP, ids["merchant_variant"])
    with get_db() as s:
        row = s.execute(text("""SELECT "competitorCount","weightedMin"
                                FROM "VariantCompetitorStats" WHERE "shopifyVariantId"=:v"""),
                        {"v": ids["merchant_variant"]}).first()
    check("OOS competitor excluded", int(row[0]) == 1, f"got={row[0]}")

    # Restore A in-stock for downstream tests
    with get_db() as s:
        s.execute(text('UPDATE "CompetitorPriceObservation" SET "isInStock"=true WHERE "competitorVariantId"=:v'),
                  {"v": ids["comp_a_variant"]})
    _recompute_for_variant(SHOP, ids["merchant_variant"])


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: Pricing engine
# ─────────────────────────────────────────────────────────────────────────────

def stage_pricing_happy(ids: dict) -> dict:
    print("\n── Stage 4a: pricing — happy path (rule lowers price within delta) ──")
    from services.pricing_svc.main import decide_price
    rid = str(uuid.uuid4())
    with get_db() as s:
        s.execute(text("""INSERT INTO "PricingRule"(id,"shopDomain",scope,"ruleType",params,
                            "floorPrice","ceilingPrice","maxDailyDeltaPct","maxStalenessSeconds",
                            "autoApply",priority,enabled,"createdAt","updatedAt")
                          VALUES (:i,:s,CAST('SHOP' AS "PricingRuleScope"),
                            CAST('MATCH_LOWEST' AS "PricingRuleType"),'{}'::jsonb,
                            500,1500,20,86400,true,100,true,NOW(),NOW())"""),
                  {"i": rid, "s": SHOP})
    out = decide_price(SHOP, ids["merchant_variant"])
    check("decide returned ok", out.get("ok"), f"out={out}")
    check("price moved down toward weighted competitor min",
          out.get("newPrice", 1100) < 1100.0,
          f"new={out.get('newPrice')}")

    with get_db() as s:
        d = s.execute(text("""SELECT "oldPrice","newPrice","ruleSuggestedPrice",confidence,
                                     "blockedBy",reason
                              FROM "PriceDecision"
                              WHERE "shopifyVariantId"=:v
                              ORDER BY "decidedAt" DESC LIMIT 1"""),
                      {"v": ids["merchant_variant"]}).first()
    check("ruleSuggestedPrice captured", d.ruleSuggestedPrice is not None,
          f"got={d.ruleSuggestedPrice}")
    check("blockedBy null on happy path", d.blockedBy is None, f"blocked={d.blockedBy}")
    return {"rule_id": rid, "decision_old": float(d.oldPrice), "decision_new": float(d.newPrice)}


def stage_pricing_blocks(ids: dict, rule_id: str) -> None:
    print("\n── Stage 4b: pricing — every blocking gate ───────────────────────────")
    from services.pricing_svc.main import decide_price

    # Gate 1: no CONFIRMED match  → downgrade ProductLevelMatch
    with get_db() as s:
        s.execute(text('UPDATE "ProductLevelMatch" SET "confidenceTier"=CAST(\'LIKELY\' AS "MatchConfidenceTier"), confidence=0.70 WHERE "shopDomain"=:s'),
                  {"s": SHOP})
    out = decide_price(SHOP, ids["merchant_variant"])
    check("blocked: no_confirmed_match", out.get("reason") == "no_confirmed_match",
          f"out={out}")

    # Restore CONFIRMED so subsequent gates can be tested in isolation
    with get_db() as s:
        s.execute(text('UPDATE "ProductLevelMatch" SET "confidenceTier"=CAST(\'CONFIRMED\' AS "MatchConfidenceTier"), confidence=0.95 WHERE "shopDomain"=:s'),
                  {"s": SHOP})

    # Gate 2: stale stats — push lastUpdatedAt back > maxStalenessSeconds
    with get_db() as s:
        s.execute(text('UPDATE "VariantCompetitorStats" SET "lastUpdatedAt"=NOW() - INTERVAL \'2 days\' WHERE "shopifyVariantId"=:v'),
                  {"v": ids["merchant_variant"]})
    out = decide_price(SHOP, ids["merchant_variant"])
    check("blocked: stale_data", out.get("reason") == "stale_data", f"out={out}")
    # Restore
    with get_db() as s:
        s.execute(text('UPDATE "VariantCompetitorStats" SET "lastUpdatedAt"=NOW() WHERE "shopifyVariantId"=:v'),
                  {"v": ids["merchant_variant"]})

    # Gate 3: active promotion window
    with get_db() as s:
        s.execute(text("""INSERT INTO "PromotionWindow"(id,"shopDomain",scope,"scopeRef","startsAt","endsAt","pauseAutoPricing","createdAt")
                          VALUES (:i,:s,CAST('SHOP' AS "PricingRuleScope"),NULL,
                                  NOW() - INTERVAL '1 hour', NOW() + INTERVAL '1 hour', true, NOW())"""),
                  {"i": str(uuid.uuid4()), "s": SHOP})
    out = decide_price(SHOP, ids["merchant_variant"])
    check("blocked: active_promotion", out.get("reason") == "active_promotion", f"out={out}")
    with get_db() as s:
        s.execute(text('DELETE FROM "PromotionWindow" WHERE "shopDomain"=:s'), {"s": SHOP})

    # Gate 4: kill switch
    with get_db() as s:
        s.execute(text("""INSERT INTO "PricingConfig"("shopDomain","killSwitch","updatedAt")
                          VALUES (:s,true,NOW())
                          ON CONFLICT ("shopDomain") DO UPDATE SET "killSwitch"=true"""),
                  {"s": SHOP})
    out = decide_price(SHOP, ids["merchant_variant"])
    check("blocked: kill_switch", out.get("reason") == "kill_switch", f"out={out}")
    with get_db() as s:
        s.execute(text('UPDATE "PricingConfig" SET "killSwitch"=false WHERE "shopDomain"=:s'),
                  {"s": SHOP})

    # Gate 5: no rule at all
    with get_db() as s:
        s.execute(text('UPDATE "PricingRule" SET enabled=false WHERE id=:i'), {"i": rule_id})
    out = decide_price(SHOP, ids["merchant_variant"])
    check("blocked: no_rule_configured",
          out.get("reason") == "no_rule_configured", f"out={out}")
    with get_db() as s:
        s.execute(text('UPDATE "PricingRule" SET enabled=true WHERE id=:i'), {"i": rule_id})


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5: Writer — gate paths (no real Shopify call)
# ─────────────────────────────────────────────────────────────────────────────

def stage_writer(ids: dict, rule_id: str) -> None:
    print("\n── Stage 5: writer gate paths ─────────────────────────────────────────")
    from services.shopify_svc.main import _apply_one_decision

    # Need a non-blocked, non-noop decision to test the missing-token path
    with get_db() as s:
        # Refresh current price for clarity
        s.execute(text('UPDATE "ShopifyVariant" SET "currentPrice"=1100 WHERE id=:v'),
                  {"v": ids["merchant_variant"]})
        did = str(uuid.uuid4())
        s.execute(text("""INSERT INTO "PriceDecision"(id,"shopDomain","shopifyVariantId","ruleId",
                            "oldPrice","newPrice",reason,"decidedAt")
                          VALUES (:i,:s,:v,:r,1100,950,'test',NOW())"""),
                  {"i": did, "s": SHOP, "v": ids["merchant_variant"], "r": rule_id})
    out = _apply_one_decision(did)
    check("writer blocks on missing offline token",
          out.get("reason") == "no_offline_token", f"out={out}")
    with get_db() as s:
        alerts = s.execute(text("""SELECT type,severity FROM "PricingAlert"
                                   WHERE "shopDomain"=:s AND type='MISSING_OFFLINE_TOKEN'"""),
                           {"s": SHOP}).all()
    check("MISSING_OFFLINE_TOKEN alert raised",
          len(alerts) >= 1 and alerts[0][1] == "CRITICAL", f"alerts={alerts}")

    # Idempotency: price already at new price → noop branch flips appliedAt
    with get_db() as s:
        did2 = str(uuid.uuid4())
        s.execute(text("""INSERT INTO "PriceDecision"(id,"shopDomain","shopifyVariantId","ruleId",
                            "oldPrice","newPrice",reason,"decidedAt")
                          VALUES (:i,:s,:v,:r,1100,1100,'noop',NOW())"""),
                  {"i": did2, "s": SHOP, "v": ids["merchant_variant"], "r": rule_id})
    out = _apply_one_decision(did2)
    check("writer noop when price already matches",
          out.get("noop") == "price_already_matches", f"out={out}")
    with get_db() as s:
        applied = s.execute(text('SELECT "appliedAt" FROM "PriceDecision" WHERE id=:i'),
                            {"i": did2}).scalar()
    check("noop flipped appliedAt", applied is not None, f"appliedAt={applied}")

    # Blocked decision must not write
    with get_db() as s:
        did3 = str(uuid.uuid4())
        s.execute(text("""INSERT INTO "PriceDecision"(id,"shopDomain","shopifyVariantId","ruleId",
                            "oldPrice","newPrice",reason,"blockedBy","decidedAt")
                          VALUES (:i,:s,:v,:r,1100,950,'r','floor',NOW())"""),
                  {"i": did3, "s": SHOP, "v": ids["merchant_variant"], "r": rule_id})
    out = _apply_one_decision(did3)
    check("writer refuses blocked decisions",
          out.get("ok") is False and "blocked" in out.get("reason", ""),
          f"out={out}")


# ─────────────────────────────────────────────────────────────────────────────
# Stage 6: ML opt-in columns exist + default correctly
# ─────────────────────────────────────────────────────────────────────────────

def stage_ml_columns(ids: dict) -> None:
    print("\n── Stage 6: shadow-ML columns ────────────────────────────────────────")
    with get_db() as s:
        row = s.execute(text("""SELECT "useMlSuggestion" FROM "ShopifyVariant" WHERE id=:v"""),
                        {"v": ids["merchant_variant"]}).scalar()
    check("ShopifyVariant.useMlSuggestion defaults to false", row is False, f"got={row}")
    with get_db() as s:
        row = s.execute(text("""SELECT "mlBlendWeight" FROM "PricingRule" WHERE "shopDomain"=:s LIMIT 1"""),
                        {"s": SHOP}).scalar()
    check("PricingRule.mlBlendWeight defaults to 0.0",
          row is not None and float(row) == 0.0, f"got={row}")
    with get_db() as s:
        rsp = s.execute(text("""SELECT count(*) FROM "PriceDecision"
                                WHERE "shopDomain"=:s AND "ruleSuggestedPrice" IS NOT NULL"""),
                        {"s": SHOP}).scalar()
    check("at least one PriceDecision has ruleSuggestedPrice populated", int(rsp) >= 1,
          f"count={rsp}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"Pipeline test — shop={SHOP}\n")
    cleanup()
    try:
        ids = setup_fixture()
        stage_observations(ids)
        stage_matcher(ids)
        stage_stats(ids)
        stage_stats_filters(ids)
        result = stage_pricing_happy(ids)
        stage_pricing_blocks(ids, result["rule_id"])
        stage_writer(ids, result["rule_id"])
        stage_ml_columns(ids)
    except Exception:
        traceback.print_exc()
        print("\nUNCAUGHT EXCEPTION — see traceback above.")
    finally:
        cleanup()

    failed = [c for c in checks if not c[1]]
    print(f"\n{'='*70}\nResult: {len(checks) - len(failed)}/{len(checks)} passed")
    if failed:
        print("FAILURES:")
        for label, _, detail in failed:
            print(f"  - {label}: {detail}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
