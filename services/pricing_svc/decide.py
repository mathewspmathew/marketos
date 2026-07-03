"""
services/pricing_svc/decide.py

Auto-pricing decision for a merchant product. Runs after fresh competitor
observations land (via stats.recompute_after_observation) and is also safe
to call directly. Pure logic — no I/O outside of one DB session + an
enqueue of pricing.apply_price when a change is decided.

Pipeline
--------
1. Eligibility gates (any failure → write a PriceDecision with skipReason
   and return; no Shopify call):
     - dynamicPricingEnabled = TRUE
     - syncPrice = TRUE
     - debounce: now - lastDecisionAt >= rescrape interval
     - ≥ minCompetitorsToPrice matched competitor PRODUCTS with:
         * MatchConfidenceTier in (CONFIRMED, LIKELY)  (WEAK excluded)
         * NOT rejectedByMerchant
         * Latest CompetitorPriceObservation within max(2× rescrape, 24h)
         * Observation currency == shop currency (INR for v1)

2. Reference price:
     - Per competitor product: median of variant prices.
     - Rank by ProductLevelMatch.confidence desc; keep top-K.
     - ref_price = Σ (weight_i × competitor_median_i),
       where weight_i = confidence_i / Σ confidence.

3. Tier bias:
     - BUDGET      → target = ref × (1 - budgetUndercut)
     - COMPETITIVE → target = ref × (1 - markupPct)         (legacy slot)
     - PREMIUM     → target = ref × (1 + premiumUplift)

4. Per-round cap: limit step to ± maxAutoApplyChangePct of current price.

5. Lifetime cap: clamp to merchant min/max if set, else to
   basePrice × (1 ± lifetimeCapPct).

6. Skip no-ops (|change| < 0.5%).

A PriceDecision row is written for EVERY run (skipped or applied) so the
stats page has full history. autoApplied is set to TRUE only when we
enqueue the Shopify push.

Variant handling: pricing is product-level. The same % delta we apply to
the product is applied uniformly to every variant of that product by
pricing.apply_price.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text

from services.common.celery_app import app
from services.common.db import get_db

logger = logging.getLogger(__name__)

# Note: MIN_CHANGE_PCT and ABS_MIN_FRESHNESS are now loaded from ShopSettings
# minChangePctThreshold and minFreshnessHours respectively

# Map frequencyUnit + interval to a timedelta for debounce + observation freshness.
_UNIT_TO_SECONDS = {
    "minute": 60,
    "hour":   3600,
    "day":    86400,
    # Legacy / settings values we still tolerate
    "daily":  86400,
}


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _rescrape_interval(freq_unit: str | None, freq_interval: int | None) -> timedelta | None:
    """Rescrape cadence as a timedelta. None means 'no schedule' (= no debounce)."""
    if not freq_unit or freq_unit == "never":
        return None
    secs = _UNIT_TO_SECONDS.get(freq_unit.lower())
    if not secs:
        return None
    return timedelta(seconds=secs * max(1, freq_interval or 1))


def _tier_target(ref_price: Decimal, tier: str, *,
                 markup: Decimal, undercut: Decimal, uplift: Decimal) -> Decimal:
    if tier == "BUDGET":
        return ref_price * (Decimal("1") - undercut)
    if tier == "PREMIUM":
        return ref_price * (Decimal("1") + uplift)
    # COMPETITIVE (default)
    return ref_price * (Decimal("1") - markup)


def _write_decision(session, *, shop_domain: str, variant_id: str,
                    old_price: Decimal, new_price: Decimal, reason: str,
                    change_pct: float | None, ref_price: Decimal | None,
                    formula_target: Decimal | None, competitors_used: int,
                    oos_observations: int, currency_drops: int,
                    top_matches: list, tier: str | None,
                    skip_reason: str | None, auto_applied: bool,
                    now: datetime) -> str:
    row = session.execute(
        text("""
            INSERT INTO "PriceDecision"
                (id, "shopDomain", "shopifyVariantId",
                 "oldPrice", "newPrice", reason,
                 "changePct", "refPrice", "formulaTarget",
                 "competitorsUsed", "oosObservations", "currencyDrops",
                 "topMatchesJson", "tierAtDecision",
                 "skipReason", "autoApplied", "decidedAt")
            VALUES
                (gen_random_uuid(), :sd, :vid,
                 :op, :np, :reason,
                 :cpct, :ref, :ftgt,
                 :cu, :oos, :cdrop,
                 CAST(:tm AS JSONB), CAST(:tier AS "PricingTier"),
                 :skip, :auto, :now)
            RETURNING id
        """),
        {
            "sd": shop_domain, "vid": variant_id,
            "op": old_price, "np": new_price, "reason": reason,
            "cpct": change_pct, "ref": ref_price, "ftgt": formula_target,
            "cu": competitors_used, "oos": oos_observations, "cdrop": currency_drops,
            "tm": json.dumps(top_matches), "tier": tier,
            "skip": skip_reason, "auto": auto_applied, "now": now,
        },
    ).first()
    return row[0]


def decide_price_for_product(shop_domain: str, shopify_product_id: str) -> dict:
    """Decide and (if eligible) enqueue an auto-apply for one product."""
    now = datetime.now(timezone.utc)

    with get_db() as session:
        product = session.execute(
            text("""
                SELECT id, "dynamicPricingEnabled", "syncPrice", "pricingTier",
                       "minPriceOverride", "maxPriceOverride",
                       "maxAutoApplyChangePctOverride", "lifetimeCapPctOverride",
                       "lastDecisionAt", "frequencyUnit", "frequencyInterval"
                FROM "ShopifyProduct"
                WHERE id = :pid AND "shopDomain" = :sd
            """),
            {"pid": shopify_product_id, "sd": shop_domain},
        ).first()
        if not product:
            return {"ok": False, "reason": "product_missing"}
        if not product.dynamicPricingEnabled:
            return {"ok": False, "reason": "pricing_disabled"}
        if not product.syncPrice:
            return {"ok": False, "reason": "syncPrice_off"}

        settings = session.execute(
            text("""
                SELECT "markupPct", "currency",
                       "minCompetitorsToPrice", "topKCompetitors",
                       "maxAutoApplyChangePct", "lifetimeCapPct",
                       "budgetUndercut", "premiumUplift",
                       "includeOosInPricing",
                       "frequencyUnit", "frequencyInterval",
                       "minChangePctThreshold", "minFreshnessHours"
                FROM "ShopSettings" WHERE "shopDomain" = :sd
            """),
            {"sd": shop_domain},
        ).first()

        # ShopSettings is required - all pricing config comes from there
        if not settings:
            return {"ok": False, "reason": "ShopSettings not found - merchant must configure pricing"}

        markup       = Decimal(str(settings.markupPct))
        min_comps    = settings.minCompetitorsToPrice
        top_k        = settings.topKCompetitors
        per_round    = Decimal(str(product.maxAutoApplyChangePctOverride
                                   if product.maxAutoApplyChangePctOverride is not None
                                   else settings.maxAutoApplyChangePct))
        lifetime_cap = Decimal(str(product.lifetimeCapPctOverride
                                   if product.lifetimeCapPctOverride is not None
                                   else settings.lifetimeCapPct))
        undercut     = Decimal(str(settings.budgetUndercut))
        uplift       = Decimal(str(settings.premiumUplift))
        shop_currency = settings.currency or "INR"
        include_oos   = bool(settings.includeOosInPricing)
        min_change_pct = Decimal(str(settings.minChangePctThreshold))
        min_freshness_hours = settings.minFreshnessHours
        tier          = product.pricingTier or "COMPETITIVE"

        # ── Debounce: at most one decision per rescrape cycle.
        freq_unit     = product.frequencyUnit     or settings.frequencyUnit
        freq_interval = product.frequencyInterval or settings.frequencyInterval
        interval      = _rescrape_interval(freq_unit, freq_interval)
        if interval and product.lastDecisionAt:
            last = product.lastDecisionAt
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < interval:
                return {"ok": False, "reason": "debounced",
                        "wait_seconds": int((interval - (now - last)).total_seconds())}

        min_freshness = timedelta(hours=min_freshness_hours)
        freshness = max(interval * 2, min_freshness) if interval else min_freshness
        fresh_cutoff = now - freshness

        # ── Variants of the merchant product (we need basePrice + currentPrice).
        variants = session.execute(
            text("""
                SELECT id, "currentPrice", "basePrice"
                  FROM "ShopifyVariant"
                 WHERE "productId" = :pid
            """),
            {"pid": shopify_product_id},
        ).all()
        if not variants:
            return {"ok": False, "reason": "no_variants"}

        # ── Pull per-competitor-product latest observations, collapse to median.
        # ProductLevelMatch.confidence drives top-K ranking AND the weight.
        # WEAK matches and merchant-rejected matches are excluded by the WHERE.
        comp_rows = session.execute(
            text("""
                WITH latest AS (
                    SELECT DISTINCT ON (sv.id)
                           sv.id              AS scraped_variant_id,
                           sv."productId"     AS scraped_product_id,
                           cpo.price          AS price,
                           cpo.currency       AS currency,
                           cpo."isInStock"    AS in_stock,
                           cpo."observedAt"   AS observed_at
                    FROM "ProductLevelMatch" plm
                    JOIN "ScrapedProduct"  sp ON sp.id = plm."scrapedProductId"
                    JOIN "ScrapedVariant"  sv ON sv."productId" = sp.id
                    LEFT JOIN "CompetitorPriceObservation" cpo
                           ON cpo."competitorVariantId" = sv.id
                    WHERE plm."shopifyProductId" = :pid
                      AND plm."shopDomain"       = :sd
                      AND plm."rejectedByMerchant" = FALSE
                      AND plm."confidenceTier" IN ('CONFIRMED', 'LIKELY')
                    ORDER BY sv.id, cpo."observedAt" DESC NULLS LAST
                )
                SELECT l.scraped_product_id,
                       l.price,
                       l.currency,
                       l.in_stock,
                       l.observed_at,
                       plm.id           AS match_id,
                       plm.confidence   AS confidence,
                       sp.title         AS title,
                       sp.domain        AS domain
                FROM latest l
                JOIN "ScrapedProduct"     sp  ON sp.id = l.scraped_product_id
                JOIN "ProductLevelMatch"  plm ON plm."scrapedProductId" = l.scraped_product_id
                                             AND plm."shopifyProductId" = :pid
                                             AND plm."shopDomain"       = :sd
            """),
            {"pid": shopify_product_id, "sd": shop_domain},
        ).all()

        # Bucket variants by competitor product; apply freshness/currency/stock filters.
        # OOS treatment is merchant-controlled (includeOosInPricing). When OFF
        # we drop the observation; either way we tally oos_count so the audit
        # row on PriceDecision shows how many were skipped.
        per_product: dict[str, dict] = {}
        oos_count = 0
        currency_drops = 0
        for r in comp_rows:
            if r.price is None:
                continue
            if r.in_stock is False:
                oos_count += 1
                if not include_oos:
                    continue
            observed_at = r.observed_at
            if observed_at is None:
                continue
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            if observed_at < fresh_cutoff:
                continue
            if (r.currency or "INR").upper() != shop_currency.upper():
                currency_drops += 1
                continue
            price = Decimal(str(r.price))
            if price <= 0:
                continue
            bucket = per_product.setdefault(r.scraped_product_id, {
                "prices": [], "confidence": float(r.confidence or 0),
                "title": r.title, "domain": r.domain, "match_id": r.match_id,
            })
            bucket["prices"].append(price)

        # Collapse each competitor product to its median variant price.
        candidates = []
        for spid, b in per_product.items():
            prices = sorted(b["prices"])
            n = len(prices)
            median = prices[n // 2] if n % 2 == 1 else (prices[n // 2 - 1] + prices[n // 2]) / Decimal("2")
            candidates.append({
                "scraped_product_id": spid,
                "match_id":   b["match_id"],
                "confidence": b["confidence"],
                "median":     median,
                "title":      b["title"],
                "domain":     b["domain"],
            })

        # Need enough COMPETITOR PRODUCTS, not variants.
        if len(candidates) < min_comps:
            # Audit-only row on the first variant so the stats page can show "why".
            v0 = variants[0]
            decision_id = _write_decision(
                session, shop_domain=shop_domain, variant_id=v0.id,
                old_price=Decimal(str(v0.currentPrice)),
                new_price=Decimal(str(v0.currentPrice)),
                reason=f"need {min_comps} competitors, have {len(candidates)}",
                change_pct=0.0, ref_price=None, formula_target=None,
                competitors_used=len(candidates), oos_observations=oos_count, currency_drops=currency_drops,
                top_matches=[], tier=tier,
                skip_reason="below_min_competitors", auto_applied=False, now=now,
            )
            return {"ok": False, "reason": "below_min_competitors",
                    "have": len(candidates), "need": min_comps}

        # Top-K by confidence, weighted average.
        candidates.sort(key=lambda c: c["confidence"], reverse=True)
        top = candidates[:max(1, top_k)]
        conf_sum = sum(c["confidence"] for c in top) or 1.0
        ref_price = sum((c["median"] * Decimal(str(c["confidence"] / conf_sum)) for c in top),
                        Decimal("0"))
        ref_price = _q(ref_price)

        formula_target = _q(_tier_target(ref_price, tier,
                                         markup=markup, undercut=undercut, uplift=uplift))

        # Anchor for per-variant scaling. formula_target was computed against
        # the product-level reference; each variant's desired price is scaled
        # by (variant.basePrice / anchor) so variant relative pricing is
        # preserved across cycles instead of every variant drifting to one
        # uniform number. Anchor = cheapest variant base, so that variant maps
        # 1:1 onto the formula target. (ShopifyProduct.avgBasePrice is display
        # only — the engine anchors on variant basePrice.)
        product_anchor = min((Decimal(str(v.basePrice or v.currentPrice))
                              for v in variants), default=Decimal("1"))
        if product_anchor <= 0:
            product_anchor = Decimal("1")

        # Apply per-round + lifetime cap PER VARIANT (each may have its own basePrice).
        written = 0
        applied_any = False
        first_decision_id: str | None = None
        for v in variants:
            cur = Decimal(str(v.currentPrice))
            if cur <= 0:
                continue
            v_base = Decimal(str(v.basePrice)) if v.basePrice is not None else cur

            # Per-variant target: scale the product-level formula by this
            # variant's anchor ratio. If a product has variants at 30 and 50,
            # both move by the same percentage from their own base, not toward
            # the same absolute number.
            v_ratio = v_base / product_anchor
            desired = _q(formula_target * v_ratio)

            # Per-round cap: cap the step to ±per_round of currentPrice.
            step = desired - cur
            max_step = cur * per_round
            if step > max_step:
                step = max_step
            elif step < -max_step:
                step = -max_step
            new_price = _q(cur + step)
            skip_reason: str | None = None
            if abs(step) >= max_step and desired != cur + step:
                skip_reason = "clamped_per_round"

            # Lifetime cap: explicit min/max → basePrice ± lifetimeCapPct → no cap.
            floor = (Decimal(str(product.minPriceOverride))
                     if product.minPriceOverride is not None
                     else v_base * (Decimal("1") - lifetime_cap))
            ceiling = (Decimal(str(product.maxPriceOverride))
                       if product.maxPriceOverride is not None
                       else v_base * (Decimal("1") + lifetime_cap))
            if new_price < floor:
                new_price = _q(floor)
                skip_reason = "clamped_lifetime_cap"
            if new_price > ceiling:
                new_price = _q(ceiling)
                skip_reason = "clamped_lifetime_cap"

            # No-op guard: skip applying price if change is below threshold.
            if cur > 0 and abs(new_price - cur) / cur < min_change_pct:
                top_matches = [{"matchId": c["match_id"], "scrapedProductId": c["scraped_product_id"],
                                  "domain": c["domain"], "confidence": c["confidence"],
                                  "median": str(c["median"])} for c in top]
                decision_id = _write_decision(
                    session, shop_domain=shop_domain, variant_id=v.id,
                    old_price=cur, new_price=cur,
                    reason=f"no_op: ref={ref_price} target={formula_target} tier={tier}",
                    change_pct=0.0, ref_price=ref_price, formula_target=formula_target,
                    competitors_used=len(top), oos_observations=oos_count, currency_drops=currency_drops,
                    top_matches=top_matches,
                    tier=tier, skip_reason="no_change", auto_applied=False, now=now,
                )
                written += 1
                continue

            change_pct = float((new_price - cur) / cur)
            top_matches = [{"matchId": c["match_id"], "scrapedProductId": c["scraped_product_id"],
                              "domain": c["domain"], "confidence": c["confidence"],
                              "median": str(c["median"])} for c in top]
            decision_id = _write_decision(
                session, shop_domain=shop_domain, variant_id=v.id,
                old_price=cur, new_price=new_price,
                reason=(f"ref={ref_price} target={formula_target} "
                        f"tier={tier} comps={len(top)}"),
                change_pct=change_pct, ref_price=ref_price, formula_target=formula_target,
                competitors_used=len(top), oos_observations=oos_count, currency_drops=currency_drops,
                top_matches=top_matches,
                tier=tier, skip_reason=skip_reason, auto_applied=True, now=now,
            )
            if first_decision_id is None:
                first_decision_id = decision_id
            applied_any = True
            written += 1

        # Stamp lastDecisionAt regardless (debounce uses this as the cycle marker).
        session.execute(
            text('UPDATE "ShopifyProduct" SET "lastDecisionAt" = :now WHERE id = :pid'),
            {"now": now, "pid": shopify_product_id},
        )

    # Enqueue the Shopify push outside the DB session. One per product, not per
    # variant — apply_price fans out to all variants of the product internally.
    if applied_any and first_decision_id:
        app.send_task(
            "pricing.apply_price",
            args=[shop_domain, shopify_product_id, first_decision_id],
            queue="pricing_queue",
        )

    return {
        "ok": True, "written": written,
        "ref_price": str(ref_price), "target": str(formula_target),
        "competitors": len(top), "tier": tier, "applied": applied_any,
    }
