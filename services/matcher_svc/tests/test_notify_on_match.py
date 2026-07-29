"""services/matcher_svc/tests/test_notify_on_match.py

Verifies match_for_scraped_product fires a Postgres NOTIFY on
'matches_channel' with the shop domain as payload, once per task run,
only when it actually wrote a match. This is the signal the live-updates
SSE feature (services/api_gateway/live_updates.py) listens for.
"""
import os
import uuid

import psycopg
import pytest
from sqlalchemy import text

from services.common.db import get_db
from services.common import models
from services.matcher_svc.main import match_for_scraped_product

_DIM = 768


def _vec(value: float) -> str:
    return "[" + ",".join([str(value)] * _DIM) + "]"


def _raw_dsn():
    url = os.environ["DATABASE_URL"]
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


@pytest.fixture
def seeded_match_pair():
    shop = f"notify-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    variant_id = f"gid://shopify/ProductVariant/{uuid.uuid4().hex[:8]}"
    scraped_id = str(uuid.uuid4())
    scraped_variant_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Notify Test Product",
            vendor="SameBrand", productType="Widget",
        ))
        s.add(models.ShopifyVariant(
            id=variant_id, productId=product_id, title="Default", currentPrice=100.00,
        ))
        s.add(models.ScrapedProduct(
            id=scraped_id, shopDomain=shop, domain="comp.example.com",
            title="Competitor Widget", vendor="SameBrand", productType="Widget",
        ))
        s.flush()
        s.add(models.ScrapedVariant(
            id=scraped_variant_id, productId=scraped_id, title="Default",
            currentPrice=95.00, currency="INR",
        ))
        s.add(models.CompetitorCandidate(
            id=candidate_id, shopDomain=shop, shopifyProductId=product_id,
            url="https://comp.example.com/widget", domain="comp.example.com",
            source="manual", status="VERIFIED", scrapedProductId=scraped_id,
        ))
        s.flush()
        s.execute(
            text(
                'INSERT INTO "ShopifyEmbedding" (id, "variantId", "shopDomain", "vectorText", "embeddedAt", "updatedAt") '
                'VALUES (:id, :vid, :sd, CAST(:vec AS vector), NOW(), NOW())'
            ),
            {"id": str(uuid.uuid4()), "vid": variant_id, "sd": shop, "vec": _vec(0.1)},
        )
        s.execute(
            text(
                'INSERT INTO "ProductEmbedding" (id, "prodId", "variantId", "shopDomain", "vectorText", "vectorizedAt") '
                'VALUES (:id, :pid, :vid, :sd, CAST(:vec AS vector), NOW())'
            ),
            {"id": str(uuid.uuid4()), "pid": scraped_id, "vid": scraped_variant_id, "sd": shop, "vec": _vec(0.1)},
        )

    yield shop, scraped_id

    with get_db() as s:
        s.execute(text('DELETE FROM "ShopifyEmbedding" WHERE "variantId" = :vid'), {"vid": variant_id})
        s.execute(text('DELETE FROM "ProductEmbedding" WHERE "prodId" = :pid'), {"pid": scraped_id})
        s.query(models.ProductMatch).filter(models.ProductMatch.shopifyVariantId == variant_id).delete(synchronize_session=False)
        s.query(models.ProductLevelMatch).filter(models.ProductLevelMatch.shopifyProductId == product_id).delete(synchronize_session=False)
        s.query(models.CompetitorCandidate).filter(models.CompetitorCandidate.id == candidate_id).delete(synchronize_session=False)
        s.query(models.ScrapedVariant).filter(models.ScrapedVariant.id == scraped_variant_id).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)
        s.query(models.ShopifyVariant).filter(models.ShopifyVariant.id == variant_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


@pytest.fixture
def seeded_shop_with_no_candidates():
    shop = f"notify-empty-{uuid.uuid4().hex[:8]}.myshopify.com"
    scraped_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ScrapedProduct(
            id=scraped_id, shopDomain=shop, domain="comp.example.com",
            title="Uncandidated Widget", vendor="SomeBrand", productType="Widget",
        ))

    yield shop, scraped_id

    with get_db() as s:
        s.query(models.ScrapedProduct).filter(models.ScrapedProduct.id == scraped_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_notify_fired_when_match_written(seeded_match_pair):
    shop_domain, scraped_product_id = seeded_match_pair

    listen_conn = psycopg.connect(_raw_dsn(), autocommit=True)
    listen_conn.execute("LISTEN matches_channel")

    match_for_scraped_product.run(scraped_product_id)

    listen_conn.execute("SELECT 1")  # round-trip so queued notifies are delivered
    notifies = list(listen_conn.notifies(timeout=5))
    listen_conn.close()

    assert any(
        n.channel == "matches_channel" and n.payload == shop_domain
        for n in notifies
    ), f"expected a matches_channel notify for {shop_domain}, got {notifies}"


def test_no_notify_when_nothing_written(seeded_shop_with_no_candidates):
    shop_domain, scraped_product_id = seeded_shop_with_no_candidates

    listen_conn = psycopg.connect(_raw_dsn(), autocommit=True)
    listen_conn.execute("LISTEN matches_channel")

    match_for_scraped_product.run(scraped_product_id)

    listen_conn.execute("SELECT 1")
    notifies = list(listen_conn.notifies(timeout=2))
    listen_conn.close()

    assert notifies == [], f"expected no notify, got {notifies}"
