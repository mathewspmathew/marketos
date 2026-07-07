# services/chatbot_svc/evals/run.py
"""Offline eval run: build golden cases from the dev DB, run each through the
real agent (real Groq calls), score with the five layer evaluators,
write reports/<ts>.json, reports/latest.json and docs/evals/CHATBOT_EVAL_REPORT.md.

Usage: uv run python -m services.chatbot_svc.evals.run
Env:   DATABASE_URL, GROQ_API_KEY, CHATBOT_RR_URL, INTERNAL_API_TOKEN,
       EVAL_SHOP_DOMAIN (dev shop with the seeded stationery products).
Logfire: agent.py already configures send_to_logfire="if-token-present";
set LOGFIRE_TOKEN to get traces, otherwise spans go to stderr only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import logfire
from pydantic_evals import Dataset

from services.chatbot_svc.evals.cases import build_cases
from services.chatbot_svc.evals.evaluators import (
    BusinessLogic,
    OutputCorrectness,
    PriceHallucination,
    StructuredOutput,
    ToolPrecision,
    ToolRecall,
    ToolSelection,
    ToolSuccess,
)
from services.chatbot_svc.evals.report import build_report, render_markdown
from services.chatbot_svc.evals.runner import ChatRunOutput, run_chat_case
from services.common.db import get_db
from services.common.models import ChatSession

# Import agent + prompt so we can log them as eval metadata.
# Importing here (after logfire.configure in agent.py) is safe; the module
# is cached so it is not re-initialised on subsequent imports.
from services.chatbot_svc.agent import agent as _agent, _PROMPT as _SYSTEM_PROMPT

_REQUIRED_ENV = ["DATABASE_URL", "GROQ_API_KEY", "CHATBOT_RR_URL", "INTERNAL_API_TOKEN",
                 "EVAL_SHOP_DOMAIN", "CHATBOT_MODEL"]
_REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS_DIR = Path(__file__).parent / "reports"
MARKDOWN_PATH = _REPO_ROOT / "docs" / "evals" / "CHATBOT_EVAL_REPORT.md"
_MODEL = os.environ.get("CHATBOT_MODEL", "")


def main() -> int:
    missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        print(f"missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 1

    shop = os.environ["EVAL_SHOP_DOMAIN"]
    cases = build_cases(shop)
    if not cases:
        print(f"no products found for shop {shop} — seed the dev store first", file=sys.stderr)
        return 1
    print(f"running {len(cases)} cases against {shop} with {_MODEL}")

    session_id = f"eval-{uuid.uuid4().hex[:12]}"

    # ------------------------------------------------------------------
    # Logfire: emit a config span BEFORE any cases run so Logfire stores
    # exactly what the agent looked like for this eval run.
    # ------------------------------------------------------------------
    _prompt_sha = hashlib.sha256(_SYSTEM_PROMPT.encode()).hexdigest()[:16]
    _tool_names = sorted(_agent._function_toolset.tools)  # noqa: SLF001
    logfire.info(
        "eval_run_config",
        model=_MODEL,
        shop=shop,
        session_id=session_id,
        cases_total=len(cases),
        python_version=sys.version,
        # System-prompt metadata (full text + fingerprint)
        system_prompt=_SYSTEM_PROMPT,
        system_prompt_chars=len(_SYSTEM_PROMPT),
        system_prompt_sha256=_prompt_sha,
        # Tool registry
        registered_tools=_tool_names,
        registered_tools_count=len(_tool_names),
    )
    # ------------------------------------------------------------------
    try:
        with get_db() as s:
            # timestamps set explicitly: the Prisma-managed table has no DB
            # default for updatedAt (matches the session-create path in app.py)
            now = datetime.now(timezone.utc)
            s.add(ChatSession(id=session_id, shopDomain=shop, title="eval run",
                              createdAt=now, updatedAt=now))
    except Exception as exc:  # noqa: BLE001 — e.g. FK failure for unregistered shop
        print(f"could not create eval ChatSession for {shop}: {exc}", file=sys.stderr)
        return 1

    try:
        dataset = Dataset(
            name="chatbot_eval",
            cases=cases,
            evaluators=[
                OutputCorrectness(),
                StructuredOutput(),
                ToolSelection(),
                PriceHallucination(),
                BusinessLogic(),
                ToolRecall(),
                ToolPrecision(),
                ToolSuccess(),
            ],
        )

        async def task(prompt: str) -> ChatRunOutput:
            return await run_chat_case(prompt, shop, session_id)

        # serial: Groq free-tier rate limits 429 under any parallel load
        lib_report = dataset.evaluate_sync(task, max_concurrency=1)
        lib_report.print(include_input=True, include_output=False)
    finally:
        with get_db() as s:  # cascade removes eval ChatPreview/ChatMessage rows
            row = s.get(ChatSession, session_id)
            if row:
                s.delete(row)

    case_dicts = []
    for rc in lib_report.cases:
        out: ChatRunOutput = rc.output
        meta = rc.metadata or {}
        case_dicts.append({
            "case_id": rc.name,
            "prompt": rc.inputs,
            "reply": out.reply if out else "",
            "ask": out.ask if out else None,
            "expected_facts": meta.get("expected_facts", []),
            "expected_tools": meta.get("expected_tools", []),
            "actual_tools": out.tool_names() if out else [],
            "tool_errors": out.tool_errors if out else [],
            "assertions": {n: a.value for n, a in (rc.assertions or {}).items()},
            "assertion_reasons": {n: a.reason for n, a in (rc.assertions or {}).items()},
            "duration_s": rc.task_duration,
            "input_tokens": out.input_tokens if out else 0,
            "output_tokens": out.output_tokens if out else 0,
            "error": out.error if out else "NoOutput",
            "retries": out.retries if out else 0,
        })

    report = build_report(case_dicts, model=_MODEL)

    # --- Logfire: summary span with all aggregate metrics + prompt metadata ---
    # pydantic-evals already emits per-case spans; this adds a summary event
    # that you can pin as a Logfire dashboard panel and correlate by
    # system_prompt_sha256 across runs.
    m = report["metrics"]
    logfire.info(
        "eval_run_summary",
        model=_MODEL,
        shop=shop,
        session_id=session_id,
        cases_total=report["cases_total"],
        overall_pass_rate_pct=report["overall_pass_rate_pct"],
        # tool quality
        tool_recall_avg=m["tool_recall_avg"],
        tool_precision_avg=m["tool_precision_avg"],
        # reliability
        error_rate_pct=m["error_rate_pct"],
        failures=report["failures"],
        # latency
        latency_p50_ms=m["latency_ms"]["p50"],
        latency_p95_ms=m["latency_ms"]["p95"],
        # token cost
        tokens_input=report["tokens"]["input"],
        tokens_output=report["tokens"]["output"],
        # prompt version — lets you correlate metric changes to prompt edits
        system_prompt_sha256=_prompt_sha,
        system_prompt_chars=len(_SYSTEM_PROMPT),
    )
    # --------------------------------------------------------------------------

    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = json.dumps(report, indent=2, default=str)
    (REPORTS_DIR / f"{ts}.json").write_text(payload)
    tmp = REPORTS_DIR / "latest.json.tmp"
    tmp.write_text(payload)
    tmp.rename(REPORTS_DIR / "latest.json")  # atomic: endpoint never sees a partial file
    MARKDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKDOWN_PATH.write_text(render_markdown(report))

    print(f"\nwrote {REPORTS_DIR / 'latest.json'}")
    print(f"wrote {MARKDOWN_PATH}")
    print(f"overall pass rate: {report['overall_pass_rate_pct']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
