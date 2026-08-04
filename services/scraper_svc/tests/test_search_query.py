"""
services/scraper_svc/tests/test_search_query.py

Unit tests for _groq_search_query's output shaping: exact_phrase gets quoted,
attributes and the fixed exclusion terms are appended unquoted, and failure
modes (missing exact_phrase, Groq errors) return None.
"""
import json
from unittest.mock import MagicMock

import services.scraper_svc.semantics as sem


def _mock_response(payload: dict):
    resp = MagicMock()
    resp.choices[0].message.content = json.dumps(payload)
    return resp


def test_query_quotes_exact_phrase_and_appends_attributes(monkeypatch):
    monkeypatch.setattr(
        sem.shopify_semantic_router, "completion",
        lambda **k: _mock_response({
            "exact_phrase": "Philips HD9200 Air Fryer",
            "attributes": ["4.1L", "Black"],
        }),
    )
    q = sem._groq_search_query(title="x", vendor="Philips", category="Appliances", description=None)
    assert q == '"philips hd9200 air fryer" 4.1l black -review -blog -forum'


def test_query_with_no_attributes(monkeypatch):
    monkeypatch.setattr(
        sem.shopify_semantic_router, "completion",
        lambda **k: _mock_response({"exact_phrase": "cotton formal shirt", "attributes": []}),
    )
    q = sem._groq_search_query(title="x", vendor=None, category="Shirts", description=None)
    assert q == '"cotton formal shirt" -review -blog -forum'


def test_query_missing_exact_phrase_returns_none(monkeypatch):
    monkeypatch.setattr(
        sem.shopify_semantic_router, "completion",
        lambda **k: _mock_response({"exact_phrase": "", "attributes": ["blue"]}),
    )
    assert sem._groq_search_query(title="x", vendor=None, category=None, description=None) is None


def test_query_groq_failure_returns_none(monkeypatch):
    def _raise(**k):
        raise RuntimeError("groq down")
    monkeypatch.setattr(sem.shopify_semantic_router, "completion", _raise)
    assert sem._groq_search_query(title="x", vendor=None, category=None, description=None) is None
