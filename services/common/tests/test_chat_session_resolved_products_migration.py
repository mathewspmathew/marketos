import uuid
from datetime import datetime, timezone

from services.common.db import get_db
from services.common.models import ChatSession, ShopifyUser


def test_chat_session_resolved_product_ids_defaults_to_empty_list():
    shop = f"resolved-ids-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    session_id = uuid.uuid4().hex

    with get_db() as s:
        s.add(ShopifyUser(shopDomain=shop))
        s.flush()
        # updatedAt has no DB default (matches the session-create path in
        # chatbot_svc/app.py), so it must be set explicitly.
        s.add(ChatSession(id=session_id, shopDomain=shop, updatedAt=datetime.now(timezone.utc)))

    with get_db() as s:
        session = s.get(ChatSession, session_id)
        assert session.resolvedProductIds == []

    with get_db() as s:
        s.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)
        s.query(ShopifyUser).filter(ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_chat_session_resolved_product_ids_round_trips():
    shop = f"resolved-ids-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    session_id = uuid.uuid4().hex

    with get_db() as s:
        s.add(ShopifyUser(shopDomain=shop))
        s.flush()
        s.add(ChatSession(id=session_id, shopDomain=shop, updatedAt=datetime.now(timezone.utc)))

    with get_db() as s:
        session = s.get(ChatSession, session_id)
        session.resolvedProductIds = ["gid://shopify/Product/1", "gid://shopify/Product/2"]

    with get_db() as s:
        session = s.get(ChatSession, session_id)
        assert session.resolvedProductIds == ["gid://shopify/Product/1", "gid://shopify/Product/2"]

    with get_db() as s:
        s.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)
        s.query(ShopifyUser).filter(ShopifyUser.shopDomain == shop).delete(synchronize_session=False)
