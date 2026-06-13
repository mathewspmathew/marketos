from datetime import datetime, timezone, timedelta
import uuid
import pytest

from services.chatbot_svc.prune import prune_old_sessions
from services.common.db import get_db
from services.common.models import ChatSession, ShopifyUser


@pytest.fixture
def shop_for_prune():
    shop = f"prune-{uuid.uuid4().hex[:8]}.myshopify.com"
    with get_db() as s:
        s.add(ShopifyUser(shopDomain=shop))
    yield shop
    with get_db() as s:
        s.query(ShopifyUser).filter_by(shopDomain=shop).delete()


def test_prune_removes_old_sessions(shop_for_prune):
    old_id = uuid.uuid4().hex
    fresh_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    with get_db() as s:
        s.add(ChatSession(id=old_id, shopDomain=shop_for_prune,
                          createdAt=now - timedelta(days=120),
                          updatedAt=now - timedelta(days=120)))
        s.add(ChatSession(id=fresh_id, shopDomain=shop_for_prune,
                          createdAt=now, updatedAt=now))

    removed = prune_old_sessions(days=90)
    assert removed >= 1
    with get_db() as s:
        assert s.get(ChatSession, old_id) is None
        assert s.get(ChatSession, fresh_id) is not None
        s.delete(s.get(ChatSession, fresh_id))
