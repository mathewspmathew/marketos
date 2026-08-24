import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from services.common.db import get_db
from services.common import models
from services.scraper_svc import celery_beat


@pytest.fixture
def seeded_queued_job():
    shop = f"beat-discovery-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    job_id = str(uuid.uuid4())

    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id, shopDomain=shop, title="Beat Discovery Test",
            discoveryNumResults=25,
        ))
        s.add(models.DiscoveryJob(
            id=job_id, shopDomain=shop, shopifyProductId=product_id,
            status="QUEUED", query="wireless mouse",
        ))

    yield shop, product_id, job_id

    with get_db() as s:
        s.query(models.DiscoveryJob).filter(models.DiscoveryJob.shopifyProductId == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_tick_dispatches_with_products_configured_num_results(seeded_queued_job, monkeypatch):
    shop, product_id, job_id = seeded_queued_job
    send = MagicMock()
    monkeypatch.setattr(celery_beat.app, "send_task", send)

    celery_beat._tick_queued_discovery_jobs()

    send.assert_called_once_with(
        "discovery.search_products",
        args=[product_id, "wireless mouse", 25, job_id],
        queue="discovery_queue",
    )


def test_tick_falls_back_to_10_when_product_has_no_configured_count(monkeypatch):
    shop = f"beat-discovery-fallback-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"gid://shopify/Product/{uuid.uuid4().hex[:8]}"
    job_id = str(uuid.uuid4())
    with get_db() as s:
        s.add(models.ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(models.ShopifyProduct(id=product_id, shopDomain=shop, title="No Count Configured"))
        s.add(models.DiscoveryJob(id=job_id, shopDomain=shop, shopifyProductId=product_id, status="QUEUED", query="usb cable"))

    # SQLAlchemy's Column(..., default=10) fires client-side on INSERT whenever
    # the field is omitted, so the row above was already written with
    # discoveryNumResults=10 rather than NULL. Force it to NULL at the DB level
    # (matching the real, nullable-with-no-default Postgres column) so this test
    # genuinely exercises the fallback branch instead of just re-reading a 10
    # that was already there.
    with get_db() as s:
        s.execute(
            text('UPDATE "ShopifyProduct" SET "discoveryNumResults" = NULL WHERE id = :pid'),
            {"pid": product_id},
        )

    send = MagicMock()
    monkeypatch.setattr(celery_beat.app, "send_task", send)

    celery_beat._tick_queued_discovery_jobs()

    send.assert_called_once_with(
        "discovery.search_products",
        args=[product_id, "usb cable", 10, job_id],
        queue="discovery_queue",
    )

    with get_db() as s:
        s.query(models.DiscoveryJob).filter(models.DiscoveryJob.shopifyProductId == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(models.ShopifyProduct.id == product_id).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(models.ShopifyUser.shopDomain == shop).delete(synchronize_session=False)
