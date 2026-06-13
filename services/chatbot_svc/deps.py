from __future__ import annotations
import os
from dataclasses import dataclass
import httpx


@dataclass
class AgentDeps:
    """Per-request dependencies threaded through Pydantic-AI tools."""
    shop_domain: str
    user_id: str | None
    session_id: str
    rr_base_url: str
    internal_token: str
    http: httpx.AsyncClient


def build_deps(shop_domain: str, user_id: str | None, session_id: str) -> AgentDeps:
    return AgentDeps(
        shop_domain=shop_domain,
        user_id=user_id,
        session_id=session_id,
        rr_base_url=os.environ["CHATBOT_RR_URL"],
        internal_token=os.environ["INTERNAL_API_TOKEN"],
        http=httpx.AsyncClient(timeout=30.0),
    )
