import pytest

from services.common.db import get_db
from services.common.models import (
    ShopifyProduct, CompetitorCandidate, ScrapedProduct,
)
from services.chatbot_svc.tools.query_studio import gather_grounding


def _product_id(shop):
    with get_db() as s:
        return s.query(ShopifyProduct.id).filter(ShopifyProduct.shopDomain == shop).scalar()


def test_gather_grounding_product_facts(seed_shop):
    g = gather_grounding(seed_shop, _product_id(seed_shop))
    assert g is not None
    assert g.title == "Boat Speaker White"
    assert g.vendor == "Boat"
    assert g.product_type == "audio"
    assert "white" in g.tags
    assert g.known_brands == []


def test_gather_grounding_includes_known_brands(seed_shop):
    pid = _product_id(seed_shop)
    with get_db() as s:
        sp = ScrapedProduct(shopDomain=seed_shop, domain="rival.example", title="Rival")
        s.add(sp); s.flush()
        scraped_id = sp.id
        s.add(CompetitorCandidate(
            shopDomain=seed_shop, shopifyProductId=pid, url="https://rival.example/p",
            domain="rival.example", source="serper_search", status="SCRAPED",
            scrapedProductId=scraped_id,
        ))
    try:
        g = gather_grounding(seed_shop, pid)
        assert "rival.example" in g.known_brands
    finally:
        with get_db() as s:
            s.query(ScrapedProduct).filter(ScrapedProduct.id == scraped_id).delete(
                synchronize_session=False
            )


def test_gather_grounding_other_shop_returns_none(seed_shop, seed_other_shop):
    assert gather_grounding(seed_other_shop, _product_id(seed_shop)) is None


class _FakeMsg:
    def __init__(self, content): self.message = type("M", (), {"content": content})


class _FakeResp:
    def __init__(self, content): self.choices = [_FakeMsg(content)]


def _fake_client(content):
    """Return an object shaped like the groq client returning `content`."""
    class _C:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    _C.last_kwargs = kwargs
                    return _FakeResp(content)
    return _C


_GOOD = '{"candidates":[{"query":"slim chino pants","confidence":8,"reason":"a"},' \
        '{"query":"stretch chinos men","confidence":6,"reason":"b"},' \
        '{"query":"cotton trousers slim","confidence":5,"reason":"c"}]}'


def test_propose_queries_returns_three(seed_shop):
    from services.chatbot_svc.tools import query_studio as qs
    pid = _product_id(seed_shop)
    out = qs.propose_queries(seed_shop, pid, "focus on chinos", n=3,
                             use_serper=False, client=_fake_client(_GOOD))
    assert [c.query for c in out] == ["slim chino pants", "stretch chinos men", "cotton trousers slim"]
    assert out[0].confidence == 8


def test_propose_queries_clamps_confidence(seed_shop):
    from services.chatbot_svc.tools import query_studio as qs
    pid = _product_id(seed_shop)
    content = '{"candidates":[{"query":"x","confidence":99,"reason":"a"},' \
              '{"query":"y","confidence":-3,"reason":"b"}]}'
    out = qs.propose_queries(seed_shop, pid, "", n=3, use_serper=False,
                             client=_fake_client(content))
    assert out[0].confidence == 10
    assert out[1].confidence == 0


def test_propose_queries_includes_focus_and_facts(seed_shop):
    from services.chatbot_svc.tools import query_studio as qs
    pid = _product_id(seed_shop)
    client = _fake_client(_GOOD)
    qs.propose_queries(seed_shop, pid, "premium only", n=3, use_serper=False, client=client)
    user_msg = client.last_kwargs["messages"][1]["content"]
    assert "premium only" in user_msg
    assert "Boat Speaker White" in user_msg


def test_serper_called_only_when_brands_sparse(seed_shop, monkeypatch):
    from services.chatbot_svc.tools import query_studio as qs
    pid = _product_id(seed_shop)
    calls = {"n": 0}

    class _Hit:
        def __init__(self, d): self.domain = d
    def fake_search(q, num=10):
        calls["n"] += 1
        return [_Hit("websearch-brand.example")]
    monkeypatch.setattr(qs, "search_web", fake_search)

    client = _fake_client(_GOOD)
    qs.propose_queries(seed_shop, pid, "", n=3, use_serper=True, client=client)
    assert calls["n"] == 1
    assert "websearch-brand.example" in client.last_kwargs["messages"][1]["content"]


def test_propose_queries_other_shop_empty(seed_shop, seed_other_shop):
    from services.chatbot_svc.tools import query_studio as qs
    out = qs.propose_queries(seed_other_shop, _product_id(seed_shop), "", n=3,
                             use_serper=False, client=_fake_client(_GOOD))
    assert out == []


@pytest.mark.parametrize("content", ["not json at all", '["array","top","level"]', '{"nope":1}'])
def test_propose_queries_malformed_json_returns_empty(seed_shop, content):
    """Malformed / unexpected LLM output degrades to [] instead of crashing."""
    from services.chatbot_svc.tools import query_studio as qs
    out = qs.propose_queries(seed_shop, _product_id(seed_shop), "", n=3,
                             use_serper=False, client=_fake_client(content))
    assert out == []


def test_propose_queries_tolerates_bad_confidence(seed_shop):
    """A float-string or null confidence must not crash; it clamps/defaults."""
    from services.chatbot_svc.tools import query_studio as qs
    content = '{"candidates":[{"query":"a","confidence":"8.5","reason":"r"},' \
              '{"query":"b","confidence":null,"reason":"r"}]}'
    out = qs.propose_queries(seed_shop, _product_id(seed_shop), "", n=3,
                             use_serper=False, client=_fake_client(content))
    assert out[0].confidence == 8
    assert out[1].confidence == 0


def test_serper_not_called_when_disabled(seed_shop, monkeypatch):
    from services.chatbot_svc.tools import query_studio as qs
    calls = {"n": 0}
    monkeypatch.setattr(qs, "search_web", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or [])
    qs.propose_queries(seed_shop, _product_id(seed_shop), "", n=3,
                       use_serper=False, client=_fake_client(_GOOD))
    assert calls["n"] == 0


def test_refine_queries_returns_candidates(seed_shop):
    from services.chatbot_svc.tools import query_studio as qs
    from services.chatbot_svc.schemas import QueryCandidate
    prior = [QueryCandidate(query="slim chinos", confidence=6, reason="x")]
    out = qs.refine_queries(seed_shop, _product_id(seed_shop), "chinos", prior,
                            "make them more premium", n=3, client=_fake_client(_GOOD))
    assert len(out) == 3
    assert out[0].query == "slim chino pants"


def test_refine_includes_prior_and_instruction(seed_shop):
    from services.chatbot_svc.tools import query_studio as qs
    from services.chatbot_svc.schemas import QueryCandidate
    client = _fake_client(_GOOD)
    prior = [QueryCandidate(query="slim chinos", confidence=6, reason="x")]
    qs.refine_queries(seed_shop, _product_id(seed_shop), "", prior,
                      "cheaper options", n=3, client=client)
    user_msg = client.last_kwargs["messages"][1]["content"]
    assert "slim chinos" in user_msg
    assert "cheaper options" in user_msg


def test_refine_other_shop_empty(seed_shop, seed_other_shop):
    from services.chatbot_svc.tools import query_studio as qs
    out = qs.refine_queries(seed_other_shop, _product_id(seed_shop), "", [], "x",
                            n=3, client=_fake_client(_GOOD))
    assert out == []
