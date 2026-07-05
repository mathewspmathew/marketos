"""Tests for get_access_token: stored-token reuse, exchange + persist, errors.

Uses a fake SQLAlchemy session (records executed statements) and a patched
requests.post — no database or network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import services.common.shopify_auth as auth


def _naive_utc(delta_seconds: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).replace(tzinfo=None)


class FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class FakeSession:
    """Returns `row` for the SELECT; records every executed statement."""

    def __init__(self, row):
        self.row = row
        self.executed = []
        self.commits = 0

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        if str(stmt).lstrip().upper().startswith("SELECT"):
            return FakeResult(self.row)
        return FakeResult(None)

    def commit(self):
        self.commits += 1

    def updates(self):
        return [(s, p) for s, p in self.executed if s.lstrip().upper().startswith("UPDATE")]


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("SHOPIFY_API_KEY", "key")
    monkeypatch.setenv("SHOPIFY_API_SECRET", "secret")


def _patch_exchange(monkeypatch, payload):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    monkeypatch.setattr(auth.requests, "post", fake_post)
    return calls


def test_reuses_stored_token_while_valid(monkeypatch):
    calls = _patch_exchange(monkeypatch, {})
    s = FakeSession(("stored-tok", _naive_utc(3600), "refresh-1", None))

    token, expires = auth.get_access_token("shop.myshopify.com", s)

    assert token == "stored-tok"
    assert expires.tzinfo is not None
    assert calls == []          # no exchange
    assert s.updates() == []    # no write
    assert s.commits == 0


def test_expired_token_exchanges_and_persists(monkeypatch):
    calls = _patch_exchange(monkeypatch, {
        "access_token": "new-tok",
        "refresh_token": "refresh-2",
        "expires_in": 3600,
        "refresh_token_expires_in": 86400,
    })
    s = FakeSession(("stale-tok", _naive_utc(-10), "refresh-1", None))

    token, _ = auth.get_access_token("shop.myshopify.com", s)

    assert token == "new-tok"
    assert len(calls) == 1
    assert calls[0][1]["json"]["refresh_token"] == "refresh-1"

    (stmt, params), = s.updates()
    assert '"accessToken"' in stmt and '"isOnline" = false' in stmt
    assert params["t"] == "new-tok"
    assert params["e"].tzinfo is None            # naive UTC for the column
    assert params["rt"] == "refresh-2"           # rotation persisted
    assert params["rte"].tzinfo is None
    assert s.commits == 1


def test_near_expiry_token_is_refreshed(monkeypatch):
    # Inside the 300s margin -> treated as expired.
    _patch_exchange(monkeypatch, {"access_token": "new-tok", "expires_in": 3600})
    s = FakeSession(("stale-tok", _naive_utc(60), "refresh-1", None))

    token, _ = auth.get_access_token("shop.myshopify.com", s)

    assert token == "new-tok"
    # No rotation in the response -> UPDATE touches only accessToken/expires.
    (_, params), = s.updates()
    assert "rt" not in params


def test_no_offline_session(monkeypatch):
    _patch_exchange(monkeypatch, {})
    with pytest.raises(auth.ShopifyAuthError, match="No offline session"):
        auth.get_access_token("shop.myshopify.com", FakeSession(None))


def test_no_refresh_token(monkeypatch):
    _patch_exchange(monkeypatch, {})
    s = FakeSession((None, None, None, None))
    with pytest.raises(auth.ShopifyAuthError, match="No refresh token"):
        auth.get_access_token("shop.myshopify.com", s)


def test_expired_refresh_token_raises(monkeypatch):
    _patch_exchange(monkeypatch, {})
    s = FakeSession((None, None, "refresh-1", _naive_utc(-10)))
    with pytest.raises(auth.ShopifyTokenExpiredError):
        auth.get_access_token("shop.myshopify.com", s)
