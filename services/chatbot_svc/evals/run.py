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

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic_evals import Dataset

from services.chatbot_svc.evals.cases import build_cases
from services.chatbot_svc.evals.evaluators import (
    BusinessLogic,
    OutputCorrectness,
    PriceHallucination,
    StructuredOutput,
    ToolSelection,
)
from services.chatbot_svc.evals.report import build_report, render_markdown
from services.chatbot_svc.evals.runner import ChatRunOutput, run_chat_case
from services.common.db import get_db
from services.common.models import ChatSession

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
            "assertions": {n: bool(a.value) for n, a in (rc.assertions or {}).items()},
            "assertion_reasons": {n: a.reason for n, a in (rc.assertions or {}).items()},
            "duration_s": rc.task_duration,
            "input_tokens": out.input_tokens if out else 0,
            "output_tokens": out.output_tokens if out else 0,
            "error": out.error if out else "NoOutput",
            "retries": out.retries if out else 0,
        })

    report = build_report(case_dicts, model=_MODEL)

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
