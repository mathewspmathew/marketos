"""One-off: run the production get_image_embedding helper against real DB rows."""
from sqlalchemy import text

from services.common.db import get_db
from services.embedding_svc.main import get_image_embedding

with get_db() as session:
    rows = session.execute(
        text('SELECT "imageUrl" FROM "ScrapedProduct" WHERE "imageUrl" IS NOT NULL LIMIT 3')
    ).all()

for (url,) in rows:
    print(f"\n[test] {url}")
    vec = get_image_embedding(url)
    if vec is None:
        print("  -> None")
    else:
        print(f"  -> ok, len={len(vec)}, sample={vec[:3]}")
