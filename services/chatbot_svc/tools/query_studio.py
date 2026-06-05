from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from services.common.db import get_db
from services.common.models import ShopifyProduct, CompetitorCandidate, ScrapedProduct
from services.common.groq_client import make_groq_client
from services.discovery_svc.serper import search_web
from services.chatbot_svc.schemas import QueryCandidate


@dataclass
class QueryGrounding:
    product_id: str
    title: str
    vendor: str | None
    product_type: str | None
    tags: list[str]
    known_brands: list[str]


def gather_grounding(shop_domain: str, product_id: str) -> QueryGrounding | None:
    """Collect real, free grounding for one product: its attributes plus the
    competitor domains we've already scraped for it. Returns None if the product
    is not in this shop."""
    with get_db() as s:
        p = s.get(ShopifyProduct, product_id)
        if p is None or p.shopDomain != shop_domain:
            return None

        brand_rows = (
            s.query(ScrapedProduct.domain)
            .join(CompetitorCandidate, CompetitorCandidate.scrapedProductId == ScrapedProduct.id)
            .filter(
                CompetitorCandidate.shopDomain == shop_domain,
                CompetitorCandidate.shopifyProductId == product_id,
            )
            .distinct()
            .all()
        )
        known_brands = [d for (d,) in brand_rows if d]

        return QueryGrounding(
            product_id=product_id,
            title=p.title,
            vendor=p.vendor,
            product_type=p.productType,
            tags=list(p.tags or []),
            known_brands=known_brands,
        )


_PROMPT = (Path(__file__).parent.parent / "prompts" / "query_studio.md").read_text()
_MODEL = os.environ.get("GROQ_REFINER_MODEL", "llama-3.3-70b-versatile")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = make_groq_client()
    return _client


def _fetch_web_brands(g: QueryGrounding, limit: int = 8) -> list[str]:
    """One Serper call to fetch real competitor brand domains, used only when we
    have no known brands. Best-effort: returns [] on any failure."""
    q = f"best {g.product_type or g.title} brands"
    try:
        hits = search_web(q, num=10)
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for h in hits:
        d = getattr(h, "domain", None)
        if d and d not in seen:
            seen.add(d)
            out.append(d)
        if len(out) >= limit:
            break
    return out


def _complete(user_payload: dict, n: int, client) -> list[QueryCandidate]:
    """Run one Groq JSON call for `user_payload` and parse up to `n` candidates.
    Degrades to [] on malformed / non-dict JSON; clamps confidence to 0-10."""
    client = client or _get_client()
    resp = client.chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
        response_format={"type": "json_object"},
        temperature=0.4,
    )
    try:
        data = json.loads(resp.choices[0].message.content)
        candidates = data.get("candidates") if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return []

    out: list[QueryCandidate] = []
    for c in (candidates or [])[:n]:
        if not isinstance(c, dict):
            continue
        query = str(c.get("query", "")).strip()
        if not query:
            continue
        try:
            conf = max(0, min(10, int(float(c.get("confidence", 0) or 0))))
        except (ValueError, TypeError):
            conf = 0
        out.append(QueryCandidate(query=query, confidence=conf, reason=str(c.get("reason", "")).strip()))
    return out


def propose_queries(
    shop_domain: str,
    product_id: str,
    focus_prompt: str,
    n: int = 3,
    *,
    use_serper: bool = True,
    client=None,
) -> list[QueryCandidate]:
    """Propose `n` competitor-search queries for a product, grounded in real data.

    Pulls product attributes + already-scraped competitor brands; if fewer than 2
    known brands and use_serper, makes ONE Serper call for real brands. Then one
    Groq JSON call returns the scored candidates. Returns [] if product not in shop.
    """
    g = gather_grounding(shop_domain, product_id)
    if g is None:
        return []

    brands = list(g.known_brands)
    if use_serper and len(brands) < 2:
        brands += _fetch_web_brands(g)

    return _complete({
        "product": {"title": g.title, "vendor": g.vendor, "type": g.product_type, "tags": g.tags},
        "known_brands": brands,
        "focus": focus_prompt or "",
        "n": n,
    }, n, client)


def refine_queries(
    shop_domain: str,
    product_id: str,
    focus_prompt: str,
    prior: list[QueryCandidate],
    instruction: str,
    n: int = 3,
    *,
    client=None,
) -> list[QueryCandidate]:
    """Revise `prior` candidates per the merchant's `instruction`, same grounding.
    No extra Serper call on refine. Returns [] if the product is not in this shop."""
    g = gather_grounding(shop_domain, product_id)
    if g is None:
        return []

    prior_payload = [{"query": c.query, "confidence": c.confidence, "reason": c.reason} for c in prior]

    return _complete({
        "product": {"title": g.title, "vendor": g.vendor, "type": g.product_type, "tags": g.tags},
        "known_brands": list(g.known_brands),
        "focus": focus_prompt or "",
        "prior": prior_payload,
        "instruction": instruction or "",
        "n": n,
    }, n, client)
