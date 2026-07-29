"""services/pricing_svc/tests/test_notify_on_decision.py

Verifies decide_price_for_product fires a Postgres NOTIFY on 'stats_channel'
with '{shop_domain}:{product_id}' as payload, on every call — including
skip/no-op runs, since those still update lastDecisionAt/decision history
that app.stats._index.jsx and app.stats.$productId.jsx display. This is the
signal services/api_gateway/live_updates.py listens for.
"""
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import psycopg
import pytest

from services.common.db import get_db
from services.common import models
from services.pricing_svc.decide import decide_price_for_product


def _raw_dsn():
    url = os.environ["DATABASE_URL"]
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


@pytest.fixture
def seeded_dynamic_pricing_product():
    shop = f"notify-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopSettings(
            shopDomain=shop, markupPct=0.1, minCompetitorsToPrice=1,
            topKCompetitors=3, maxAutoApplyChangePct=0.5, lifetimeCapPct=0.5,
            budgetUndercut=0.05, premiumUplift=0.05, includeOosInPricing=False,
            minChangePctThreshold=0.001, minFreshnessHours=999999,
        ))
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Notify Test Product",
            dynamicPricingEnabled=True, syncPrice=True, pricingTier="COMPETITIVE",
        ))
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default",
            currentPrice=100.00, basePrice=100.00,
        ))

        scraped_id = str(uuid.uuid4())
        scraped_variant_id = str(uuid.uuid4())
        s.add(models.ScrapedProduct(
            id=scraped_id, shopDomain=shop, domain="comp.example.com", title="Competitor",
        ))
        s.flush()
        s.add(models.ScrapedVariant(
            id=scraped_variant_id, productId=scraped_id, title="Default",
            currentPrice=Decimal("80.00"), currency="INR",
        ))
        s.add(models.ProductLevelMatch(
            id=str(uuid.uuid4()), shopDomain=shop,
            shopifyProductId=product_id, scrapedProductId=scraped_id,
            confidence=0.9, confidenceTier="CONFIRMED",
            reviewStatus="PENDING",
        ))
        s.flush()
        s.add(models.CompetitorPriceObservation(
            id=str(uuid.uuid4()), shopDomain=shop, competitorVariantId=scraped_variant_id,
            price=Decimal("80.00"), currency="INR", isInStock=True,
            observedAt=datetime.now(timezone.utc),
        ))

    yield shop, product_id

    with get_db() as s:
        s.query(models.CompetitorPriceObservation).filter(
            models.CompetitorPriceObservation.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ProductLevelMatch).filter(
            models.ProductLevelMatch.shopDomain == shop).delete(synchronize_session=False)
        # ScrapedVariant rows cascade-delete (ondelete="CASCADE") when their
        # parent ScrapedProduct is deleted below.
        s.query(models.ScrapedProduct).filter(
            models.ScrapedProduct.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.PriceDecision).filter(
            models.PriceDecision.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopSettings).filter(models.ShopSettings.shopDomain == shop).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_notify_fired_on_every_decide_call(seeded_dynamic_pricing_product):
    shop_domain, product_id = seeded_dynamic_pricing_product

    listen_conn = psycopg.connect(_raw_dsn(), autocommit=True)
    listen_conn.execute("LISTEN stats_channel")

    decide_price_for_product(shop_domain, product_id)

    listen_conn.execute("SELECT 1")
    notifies = list(listen_conn.notifies(timeout=5))
    listen_conn.close()

    expected_payload = f"{shop_domain}:{product_id}"
    assert any(
        n.channel == "stats_channel" and n.payload == expected_payload
        for n in notifies
    ), f"expected a stats_channel notify {expected_payload!r}, got {notifies}"
