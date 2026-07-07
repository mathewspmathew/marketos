# services/chatbot_svc/tests/test_preview_toggle_brief.py
import os
os.environ.setdefault("GROQ_API_KEY", "test")

import pytest


def test_preview_price_raises_on_empty_scope(seed_shop):
    from services.chatbot_svc.tools.preview import preview_price_change
    from services.chatbot_svc.schemas import ScopeFilter, PriceChange
    with pytest.raises(RuntimeError, match="nothing to"):
        preview_price_change(
            seed_shop, "sess-empty", ScopeFilter(product_ids=["gid://does-not-exist"]),
            PriceChange(type="percent", value=10),
        )
