from fastapi.testclient import TestClient

from services.chatbot_svc.schemas import QueryCandidate
import services.chatbot_svc.tools.query_studio as qs
from services.chatbot_svc.app import app

_client = TestClient(app)


def test_query_studio_propose(monkeypatch):
    monkeypatch.setattr(qs, "propose_queries",
                        lambda *a, **k: [QueryCandidate(query="qq", confidence=7, reason="r")])
    r = _client.post("/query-studio", json={
        "shop_domain": "s.myshopify.com", "product_id": "p", "focus": "chinos",
    })
    assert r.status_code == 200
    assert r.json()["candidates"] == [{"query": "qq", "confidence": 7, "reason": "r"}]


def test_query_studio_refine(monkeypatch):
    captured = {}
    def fake_refine(shop, pid, focus, prior, instruction, n=3, **k):
        captured["prior"] = prior
        captured["instruction"] = instruction
        return [QueryCandidate(query="refined", confidence=9, reason="r")]
    monkeypatch.setattr(qs, "refine_queries", fake_refine)
    r = _client.post("/query-studio", json={
        "shop_domain": "s.myshopify.com", "product_id": "p", "focus": "chinos",
        "mode": "refine", "instruction": "cheaper",
        "prior": [{"query": "slim chinos", "confidence": 6, "reason": "x"}],
    })
    assert r.status_code == 200
    assert r.json()["candidates"][0]["query"] == "refined"
    assert captured["instruction"] == "cheaper"
    assert captured["prior"][0].query == "slim chinos"
