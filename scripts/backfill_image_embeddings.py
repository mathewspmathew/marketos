"""Synchronous backfill of vectorImg for ProductEmbedding and ShopifyEmbedding.
Run once after the get_image_embedding fix lands."""
import time

from sqlalchemy import text

from services.common.db import get_db
from services.embedding_svc.main import get_image_embedding

# Vertex multimodalembedding default quota ~120 RPM per region. Sleep to be safe.
_SLEEP_BETWEEN_CALLS = 6.0


def _vec(values):
    return "[" + ",".join(str(x) for x in values) + "]"


def backfill_product_embeddings():
    with get_db() as session:
        rows = session.execute(
            text(
                'SELECT pe.id, sp."imageUrl" '
                'FROM "ProductEmbedding" pe '
                'JOIN "ScrapedProduct" sp ON sp.id = pe."prodId" '
                'WHERE pe."vectorImg" IS NULL AND sp."imageUrl" IS NOT NULL'
            )
        ).all()
        print(f"[product] {len(rows)} row(s) needing vectorImg")

        for pe_id, url in rows:
            vec = get_image_embedding(url)
            if not vec:
                print(f"  [skip] {pe_id[:8]} — embedding failed")
                continue
            session.execute(
                text(
                    'UPDATE "ProductEmbedding" '
                    'SET "vectorImg" = CAST(:v AS vector) '
                    'WHERE id = :id'
                ),
                {"id": pe_id, "v": _vec(vec)},
            )
            print(f"  [ok] {pe_id[:8]} -> dim={len(vec)}")
            time.sleep(_SLEEP_BETWEEN_CALLS)


def backfill_shopify_embeddings():
    with get_db() as session:
        rows = session.execute(
            text(
                'SELECT se.id, sp."imageUrl" '
                'FROM "ShopifyEmbedding" se '
                'JOIN "ShopifyVariant" sv ON sv.id = se."variantId" '
                'JOIN "ShopifyProduct" sp ON sp.id = sv."productId" '
                'WHERE se."vectorImg" IS NULL AND sp."imageUrl" IS NOT NULL'
            )
        ).all()
        print(f"[shopify] {len(rows)} row(s) needing vectorImg")

        for se_id, url in rows:
            vec = get_image_embedding(url)
            if not vec:
                print(f"  [skip] {se_id[:8]} — embedding failed")
                continue
            session.execute(
                text(
                    'UPDATE "ShopifyEmbedding" '
                    'SET "vectorImg" = CAST(:v AS vector), "updatedAt" = NOW() '
                    'WHERE id = :id'
                ),
                {"id": se_id, "v": _vec(vec)},
            )
            print(f"  [ok] {se_id[:8]} -> dim={len(vec)}")


if __name__ == "__main__":
    backfill_product_embeddings()
    backfill_shopify_embeddings()
