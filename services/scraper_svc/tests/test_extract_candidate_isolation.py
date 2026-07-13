import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from services.common.db import get_db
from services.common import models
from services.common.schemas import ProductSchema, VariantSchema
from services.scraper_svc.candidate import _upsert_scraped_product


def _make_product(shop_domain, product_id, title):
    with get_db() as s:
        # Idempotent: two products in a test can share the same shop_domain.
        s.execute(
            pg_insert(models.ShopifyUser)
            .values(shopDomain=shop_domain)
            .on_conflict_do_nothing(index_elements=["shopDomain"])
        )
        s.flush()
        s.add(models.ShopifyProduct(
            id=product_id,
            shopDomain=shop_domain,
            title=title,
            vendor="V",
            productType="t",
            tags=[],
            dynamicPricingEnabled=False,
        ))


def _cleanup(shop_domain, product_ids, scraped_product_ids):
    with get_db() as s:
        s.query(models.ScrapedVariant).filter(
            models.ScrapedVariant.productId.in_(scraped_product_ids)
        ).delete(synchronize_session=False)
        s.query(models.ProductUrl).filter(
            models.ProductUrl.shopifyProductId.in_(product_ids)
        ).delete(synchronize_session=False)
        s.query(models.ScrapedProduct).filter(
            models.ScrapedProduct.id.in_(scraped_product_ids)
        ).delete(synchronize_session=False)
        s.query(models.ShopifyProduct).filter(
            models.ShopifyProduct.id.in_(product_ids)
        ).delete(synchronize_session=False)
        s.query(models.ShopifyUser).filter(
            models.ShopifyUser.shopDomain == shop_domain
        ).delete(synchronize_session=False)


def test_two_products_same_url_get_separate_scraped_products():
    shop_domain = f"test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_a_id = f"product-a-{uuid.uuid4().hex[:8]}"
    product_b_id = f"product-b-{uuid.uuid4().hex[:8]}"
    url = f"https://competitor.example.com/item-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    schema = ProductSchema(
        title="Widget",
        description="A widget",
        vendor="Acme",
        product_type="widgets",
        tags=[],
        variants=[VariantSchema(current_price=19.99)],
    )

    _make_product(shop_domain, product_a_id, "Product A")
    _make_product(shop_domain, product_b_id, "Product B")

    scraped_ids = []
    try:
        with get_db() as db:
            prod_id_a = _upsert_scraped_product(
                db, shopify_product_id=product_a_id, shop_domain=shop_domain,
                domain="competitor.example.com", url=url, product=schema,
                image_url="", now=now,
            )
        scraped_ids.append(prod_id_a)

        with get_db() as db:
            prod_id_b = _upsert_scraped_product(
                db, shopify_product_id=product_b_id, shop_domain=shop_domain,
                domain="competitor.example.com", url=url, product=schema,
                image_url="", now=now,
            )
        scraped_ids.append(prod_id_b)

        assert prod_id_a != prod_id_b, "each product must get its own ScrapedProduct row"

        with get_db() as db:
            url_row_a = db.query(models.ProductUrl).filter(
                models.ProductUrl.shopifyProductId == product_a_id,
                models.ProductUrl.url == url,
            ).first()
            url_row_b = db.query(models.ProductUrl).filter(
                models.ProductUrl.shopifyProductId == product_b_id,
                models.ProductUrl.url == url,
            ).first()
            assert url_row_a is not None and url_row_a.prodId == prod_id_a
            assert url_row_b is not None and url_row_b.prodId == prod_id_b
    finally:
        _cleanup(shop_domain, [product_a_id, product_b_id], scraped_ids)


def test_same_product_rescraping_same_url_reuses_scraped_product():
    shop_domain = f"test-{uuid.uuid4().hex[:8]}.myshopify.com"
    product_id = f"product-{uuid.uuid4().hex[:8]}"
    url = f"https://competitor.example.com/item-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    schema_v1 = ProductSchema(
        title="Widget", description="v1", vendor="Acme", product_type="widgets",
        tags=[], variants=[VariantSchema(current_price=19.99)],
    )
    schema_v2 = ProductSchema(
        title="Widget", description="v2", vendor="Acme", product_type="widgets",
        tags=[], variants=[VariantSchema(current_price=24.99)],
    )

    _make_product(shop_domain, product_id, "Product")

    scraped_ids = []
    try:
        with get_db() as db:
            prod_id_1 = _upsert_scraped_product(
                db, shopify_product_id=product_id, shop_domain=shop_domain,
                domain="competitor.example.com", url=url, product=schema_v1,
                image_url="", now=now,
            )
        scraped_ids.append(prod_id_1)

        with get_db() as db:
            prod_id_2 = _upsert_scraped_product(
                db, shopify_product_id=product_id, shop_domain=shop_domain,
                domain="competitor.example.com", url=url, product=schema_v2,
                image_url="", now=now,
            )

        assert prod_id_2 == prod_id_1, "re-scraping the same product+url must update, not duplicate"

        with get_db() as db:
            row = db.get(models.ScrapedProduct, prod_id_1)
            assert row.description == "v2"
    finally:
        _cleanup(shop_domain, [product_id], scraped_ids)
