# services/chatbot_svc/tests/test_toggle_settings.py
import os
os.environ.setdefault("GROQ_API_KEY", "test")

from services.common.db import get_db
from services.common.models import ShopifyProduct
from services.chatbot_svc.tools.toggle_settings import resolve_enable_settings


def _product_id(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(ShopifyProduct.shopDomain == shop).scalar()


def test_resolve_enable_settings_uses_fallback_defaults(seed_shop):
    pid = _product_id(seed_shop)
    out = resolve_enable_settings(seed_shop, [pid])
    assert out["productId"] == pid
    # seed product: discoveryNumResults defaults to 10, listingExpansionCap is null,
    # no ShopSettings row -> 5. searchQuery null -> falls back to the title.
    assert out["numResults"] == 10
    assert out["listingExpansionCap"] == 5
    assert out["query"] == "Boat Speaker White"


def test_resolve_enable_settings_empty_products():
    out = resolve_enable_settings("nobody.myshopify.com", [])
    assert out == {"productId": None, "numResults": 10, "listingExpansionCap": 5, "query": ""}
