"""Re-run the matcher synchronously for one shop. Verifies hybrid pipeline end-to-end."""
from sqlalchemy import text

from services.common.db import get_db
from services.matcher_svc.main import _match_variant

SHOP = "fabric-dressing.myshopify.com"

with get_db() as session:
    variant_ids = [
        r[0] for r in session.execute(
            text(
                'SELECT "variantId" FROM "ShopifyEmbedding" '
                'WHERE "shopDomain" = :sd AND "vectorText" IS NOT NULL'
            ),
            {"sd": SHOP},
        ).all()
    ]

print(f"[run] {len(variant_ids)} variant(s) to match")
total = 0
for vid in variant_ids:
    total += _match_variant(SHOP, vid)
print(f"[done] {total} ProductMatch row(s) written/updated")
