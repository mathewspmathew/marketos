"""
services/common/groq_client.py

Groq access via litellm.Router — one router per task-type group, matching
the existing TPM-budget isolation (extraction / candidate-extraction /
competitor semantics / Shopify semantics never compete for the same key).
Each group is a list of deployments (API keys); Router load-balances across
them and puts a key on cooldown automatically on a 429, instead of us
hand-writing a new client/env var every time a group needs more headroom —
just append another env var name to that group's env-var list below.

On top of key rotation, each group also has a MODEL fallback chain
(_MODEL_CHAIN): Groq tracks TPD/RPD quota per exact model string per key, so
when every key in a group has exhausted its quota on the primary model, the
Router moves those same keys to the next model in the chain instead of
failing outright.

Each Router instance is independent, so every group reuses the same
internal tier aliases ("groq", "groq_fb1", "groq_fb2") — they only need to
be unique within a single Router's own model_list, not across groups. Call
sites use router.completion(model="groq", ...) — litellm returns an
OpenAI-compatible response, so `.choices[0].message.content` still works;
the Router transparently falls back to later tiers behind that same call.
"""
from __future__ import annotations

import os

from litellm import Router

# Tried in order: fast/cheap first, then progressively larger models. Each
# key gets a fresh TPD/RPD budget per model, so this chain buys ~3x the
# effective daily quota per key before a group is fully exhausted.
_MODEL_CHAIN = [
    ("groq",     "groq/llama-3.1-8b-instant"),
    ("groq_fb1", "groq/llama-3.3-70b-versatile"),
    ("groq_fb2", "groq/openai/gpt-oss-20b"),
]


def _deployments(env_vars: list[str], group_name: str, model: str) -> list[dict]:
    out = []
    for env_var in env_vars:
        key = os.environ.get(env_var)
        if key:
            out.append({
                "model_name": group_name,
                "litellm_params": {"model": model, "api_key": key},
            })
    return out


def _build_router(env_vars: list[str]) -> Router:
    model_list: list[dict] = []
    for group_name, model in _MODEL_CHAIN:
        model_list += _deployments(env_vars, group_name, model)
    primary_group    = _MODEL_CHAIN[0][0]
    fallback_groups  = [group for group, _ in _MODEL_CHAIN[1:]]
    return Router(model_list=model_list, fallbacks=[{primary_group: fallback_groups}])


# extraction: extract_product + rescrape_extract (extractor.py) share this.
# GROQ_API_KEY_BACKUP folds in here as a second deployment — Router's
# cooldown/rotation subsumes the old auth-failure-only backup-key switch.
extraction_router = _build_router(["GROQ_API_KEY", "GROQ_API_KEY_BACKUP"])

# candidate: extract_candidate (candidate.py) — competitor product extraction.
candidate_router = _build_router(["GROQ_API_KEY_CANDIDATE"])

# semantic: generate_variant_semantics (semantics.py) — competitor semantics.
semantic_router = _build_router(["GROQ_API_KEY_SEMANTIC"])

# shopify_semantic: generate_shopify_variant_semantics (semantics.py).
shopify_semantic_router = _build_router(["GROQ_API_KEY_SHOPIFY"])
