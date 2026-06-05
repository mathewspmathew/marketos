from datetime import datetime, timezone

from services.chatbot_svc.app import _preview_event_data
from services.common.models import ChatPreview


def test_preview_event_includes_change_and_variant_ids():
    """The SSE preview payload must carry `change` (so the card can tell enable
    from disable) and `variantIds` — regression for the enable card rendering
    the disable controls."""
    p = ChatPreview(
        id="p1",
        sessionId="s1",
        shopDomain="shop.myshopify.com",
        kind="dynamic_pricing_toggle",
        change={"enabled": True, "numResults": 2, "listingExpansionCap": 3},
        variantIds=["v1", "v2"],
        summary={"count": 1},
        expiresAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
        createdAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    d = _preview_event_data(p)
    assert d["kind"] == "dynamic_pricing_toggle"
    assert d["change"]["enabled"] is True
    assert d["change"]["numResults"] == 2
    assert d["variantIds"] == ["v1", "v2"]
    assert d["expires_at"].startswith("2026-01-01")
