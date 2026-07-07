from services.common.db import get_db
from services.common.models import ShopifyProduct, ShopifyVariant
with get_db() as s:
    prod = s.query(ShopifyProduct).filter(ShopifyProduct.title.ilike("%Compact Leather Crossbody Bag%")).first()
    print("Product:", prod.title)
    for v in prod.variants:
        print("Variant:", v.id, "Price:", v.currentPrice)
