"""
services/discovery_svc/serper.py

Thin Serper.dev client. Only the organic /search endpoint is used now —
we trust the user-supplied query and skip Google Shopping (which biases
results toward marketplaces).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx

SERPER_BASE = "https://google.serper.dev"
SERPER_TIMEOUT = 20.0


@dataclass
class CandidateHit:
    url: str
    domain: str
    title: str
    snippet: str
    source: str = "serper_search"
    price: Optional[float] = None
    position: Optional[int] = None
    raw: dict = field(default_factory=dict)


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _api_key() -> str:
    key = os.getenv("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY not set")
    return key


def search_web(
    query: str,
    num: int = 10,
    *,
    gl: Optional[str] = None,
    hl: Optional[str] = None,
    location: Optional[str] = None,
) -> list[CandidateHit]:
    """Organic /search. Over-fetch by ~3x so the caller's domain blocklist
    has room to drop noise without starving the final list."""
    payload: dict = {"q": query, "num": max(num * 3, 20)}
    if gl:       payload["gl"] = gl
    if hl:       payload["hl"] = hl
    if location: payload["location"] = location

    with httpx.Client(timeout=SERPER_TIMEOUT) as client:
        r = client.post(
            f"{SERPER_BASE}/search",
            headers={"X-API-KEY": _api_key(), "Content-Type": "application/json"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()

    out: list[CandidateHit] = []
    for item in data.get("organic", []) or []:
        url = item.get("link")
        if not url:
            continue
        out.append(CandidateHit(
            url=url,
            domain=_domain_of(url),
            title=item.get("title", "") or "",
            snippet=item.get("snippet", "") or "",
            position=item.get("position"),
            raw=item,
        ))
    return out
