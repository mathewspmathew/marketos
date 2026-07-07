"""LLM-as-judge for output correctness evaluation.

The judge receives the user's original prompt, the agent's reply, and a list
of correctness criteria derived from the case's `expected_facts`.  It returns
a float score 0.0–1.0 and a one-sentence reasoning string.

Model is configured via CHATBOT_EVAL_MODEL (Groq model string, e.g.
"groq:llama-3.3-70b-versatile").  The agent is instantiated lazily on first
call so the module can be imported even when the env var is not yet set.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_ai import Agent

# ---------------------------------------------------------------------------
# Judge output schema
# ---------------------------------------------------------------------------

class JudgeVerdict(BaseModel):
    score: float = Field(
        ge=0.0,
        le=1.0,
        description="Correctness score: 1.0 = fully correct, 0.0 = completely wrong.",
    )
    reasoning: str = Field(
        description="One concise sentence explaining the score.",
    )


# ---------------------------------------------------------------------------
# Judge system prompt
# ---------------------------------------------------------------------------

_SYSTEM = """\
You are an impartial evaluator for an AI chatbot that assists Shopify merchants
with product pricing and dynamic-pricing management.

Your task: given a merchant's question, the chatbot's reply, and a list of
correctness criteria, assign a score from 0.0 to 1.0.

Scoring rubric:
  1.0 — Reply fully and accurately satisfies every criterion.
  0.75 — Reply is mostly correct; minor omission or slight imprecision.
  0.5  — Reply partially satisfies the criteria; key info present but
          significant gaps or errors.
  0.25 — Reply is marginally relevant; mostly wrong or misleading.
  0.0  — Reply is factually wrong, refuses inappropriately, hallucinated
          data, or completely off-topic.

When the criteria list is empty, score based on overall helpfulness and
appropriateness: does the reply make sense, stay within scope, and correctly
decline out-of-scope questions?

Rules:
- Be strict: do not give 1.0 unless every criterion is clearly met.
- Do not penalise for terse replies — the chatbot is designed to be brief.
- Do not reward verbose replies that are still wrong.
- Output ONLY the JSON object with `score` and `reasoning`.
"""


# ---------------------------------------------------------------------------
# Lazy agent factory (avoids import-time env-var requirement)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_judge_agent() -> Agent[None, JudgeVerdict]:
    model = os.environ.get("CHATBOT_EVAL_MODEL", "")
    if not model:
        raise RuntimeError(
            "CHATBOT_EVAL_MODEL is not set — add it to .env, "
            "e.g. CHATBOT_EVAL_MODEL=groq:llama-3.3-70b-versatile"
        )
    return Agent(model, output_type=JudgeVerdict, system_prompt=_SYSTEM)


# ---------------------------------------------------------------------------
# Public async function called by the OutputCorrectness evaluator
# ---------------------------------------------------------------------------

async def llm_judge(
    user_prompt: str,
    agent_reply: str,
    criteria: list[str],
) -> tuple[float, str]:
    """Call the judge LLM and return (score 0–1, reasoning sentence).

    Falls back to score=0.5 / reason string if the judge itself fails,
    so a judge outage never silently masks a real eval failure.
    """
    criteria_block = (
        "\n".join(f"- {c}" for c in criteria)
        if criteria
        else "(none — evaluate overall quality and scope-adherence)"
    )
    user_message = (
        f"MERCHANT QUESTION:\n{user_prompt}\n\n"
        f"CHATBOT REPLY:\n{agent_reply or '(no reply — agent asked a clarifying question)'}\n\n"
        f"CORRECTNESS CRITERIA:\n{criteria_block}"
    )
    try:
        agent = _get_judge_agent()
        result = await agent.run(user_message)
        verdict = result.output
        return round(verdict.score, 3), verdict.reasoning
    except Exception as exc:  # noqa: BLE001
        return 0.5, f"judge error ({type(exc).__name__}): {exc}"
