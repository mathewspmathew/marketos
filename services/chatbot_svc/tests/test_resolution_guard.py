import uuid
from datetime import datetime, timezone

import pytest

from services.common.db import get_db
from services.common.models import ChatSession, ShopifyUser
from services.chatbot_svc.tools.resolution_guard import (
    record_resolved_products, ensure_product_resolved,
)


@pytest.fixture
def chat_session():
    shop = f"resolution-guard-test-{uuid.uuid4().hex[:8]}.myshopify.com"
    session_id = uuid.uuid4().hex

    with get_db() as s:
        s.add(ShopifyUser(shopDomain=shop))
        s.flush()
        now = datetime.now(timezone.utc)
        s.add(ChatSession(id=session_id, shopDomain=shop, createdAt=now, updatedAt=now))

    yield session_id

    with get_db() as s:
        s.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)
        s.query(ShopifyUser).filter(ShopifyUser.shopDomain == shop).delete(synchronize_session=False)


def test_record_then_ensure_resolved_passes(chat_session):
    record_resolved_products(chat_session, ["gid://shopify/Product/1"])
    ensure_product_resolved(chat_session, "gid://shopify/Product/1")  # must not raise


def test_ensure_resolved_raises_when_never_recorded(chat_session):
    with pytest.raises(RuntimeError, match="hasn't been resolved"):
        ensure_product_resolved(chat_session, "gid://shopify/Product/999")


def test_record_dedups_repeat_ids(chat_session):
    record_resolved_products(chat_session, ["gid://shopify/Product/1"])
    record_resolved_products(chat_session, ["gid://shopify/Product/1", "gid://shopify/Product/2"])

    with get_db() as s:
        session = s.get(ChatSession, chat_session)
        assert sorted(session.resolvedProductIds) == [
            "gid://shopify/Product/1", "gid://shopify/Product/2",
        ]


def test_record_multiple_ids_in_one_call(chat_session):
    record_resolved_products(chat_session, ["gid://shopify/Product/1", "gid://shopify/Product/2"])
    ensure_product_resolved(chat_session, "gid://shopify/Product/1")
    ensure_product_resolved(chat_session, "gid://shopify/Product/2")


def test_ensure_resolved_is_isolated_per_session():
    shop_a = f"resolution-guard-a-{uuid.uuid4().hex[:8]}.myshopify.com"
    shop_b = f"resolution-guard-b-{uuid.uuid4().hex[:8]}.myshopify.com"
    session_a = uuid.uuid4().hex
    session_b = uuid.uuid4().hex

    with get_db() as s:
        s.add(ShopifyUser(shopDomain=shop_a))
        s.add(ShopifyUser(shopDomain=shop_b))
        s.flush()
        now = datetime.now(timezone.utc)
        s.add(ChatSession(id=session_a, shopDomain=shop_a, createdAt=now, updatedAt=now))
        s.add(ChatSession(id=session_b, shopDomain=shop_b, createdAt=now, updatedAt=now))

    try:
        record_resolved_products(session_a, ["gid://shopify/Product/shared"])
        ensure_product_resolved(session_a, "gid://shopify/Product/shared")  # must not raise
        with pytest.raises(RuntimeError, match="hasn't been resolved"):
            ensure_product_resolved(session_b, "gid://shopify/Product/shared")
    finally:
        with get_db() as s:
            s.query(ChatSession).filter(ChatSession.id.in_([session_a, session_b])).delete(synchronize_session=False)
            s.query(ShopifyUser).filter(ShopifyUser.shopDomain.in_([shop_a, shop_b])).delete(synchronize_session=False)


def test_record_with_empty_list_is_a_no_op(chat_session):
    record_resolved_products(chat_session, [])  # must not raise
    with get_db() as s:
        session = s.get(ChatSession, chat_session)
        assert session.resolvedProductIds == []
