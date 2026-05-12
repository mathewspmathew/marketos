"""Demonstrate hybrid scoring path: pick a merchant bag variant (no image),
temporarily inject a vectorImg derived from a competitor bag's image, run the
matcher for that one variant, confirm matchType='hybrid' is written.
Cleans up after itself."""
from sqlalchemy import text

from services.common.db import get_db
from services.embedding_svc.main import get_image_embedding
from services.matcher_svc.main import _match_variant

SHOP = "fabric-dressing.myshopify.com"


def _vec(v):
    return "[" + ",".join(str(x) for x in v) + "]"


with get_db() as session:
    # Pick a bag merchant variant (these matched as 'semantic' before).
    target = session.execute(
        text(
            'SELECT sv.id, sp.title '
            'FROM "ShopifyVariant" sv '
            'JOIN "ShopifyProduct" sp ON sp.id = sv."productId" '
            'JOIN "ShopifyEmbedding" se ON se."variantId" = sv.id '
            'WHERE sp.title = \'Quilted Lambskin Shoulder Bag\' '
            '  AND se."vectorImg" IS NULL '
            'LIMIT 1'
        )
    ).first()
    if not target:
        print("[!] no test variant available")
        raise SystemExit(1)

    vid, ptitle = target
    print(f"[test] merchant variant: {vid} ({ptitle})")

    # Borrow a competitor image (cross-domain reuse is fine for this demo).
    comp_img = session.execute(
        text('SELECT "imageUrl" FROM "ScrapedProduct" WHERE "imageUrl" IS NOT NULL LIMIT 1')
    ).first()[0]
    print(f"[test] injecting image: {comp_img}")

    img_vec = get_image_embedding(comp_img)
    assert img_vec and len(img_vec) == 1408, "image embed failed"

    session.execute(
        text(
            'UPDATE "ShopifyEmbedding" SET "vectorImg" = CAST(:v AS vector) '
            'WHERE "variantId" = :vid'
        ),
        {"v": _vec(img_vec), "vid": vid},
    )

# Run matcher for just this variant.
written = _match_variant(SHOP, vid)
print(f"[test] matcher wrote/updated {written} row(s)")

# Inspect results.
with get_db() as session:
    rows = session.execute(
        text(
            'SELECT pm."matchType", pm."matchScore", sp.title '
            'FROM "ProductMatch" pm '
            'LEFT JOIN "ScrapedProduct" sp ON sp.id = pm."competitorProdId" '
            'WHERE pm."shopifyVariantId" = :vid '
            'ORDER BY pm."matchScore" DESC LIMIT 8'
        ),
        {"vid": vid},
    ).all()
    print(f"\n[test] top matches for this variant:")
    for r in rows:
        print(f"  {r.matchType:8s} {r.matchScore:6.2f}  {r.title[:70] if r.title else '<orphan>'}")

    # Cleanup
    session.execute(
        text(
            'UPDATE "ShopifyEmbedding" SET "vectorImg" = NULL '
            'WHERE "variantId" = :vid'
        ),
        {"vid": vid},
    )
    print("\n[test] cleaned up vectorImg injection")
