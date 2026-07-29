"""Fast, dev-only runner for the multi-turn grounding regression cases in
cases_multiturn.py. Separate from run.py's 40-case suite so tuning the
agent (prompt / validator / temperature) doesn't require a 10+ minute full
run each iteration -- once a case is consistently green here, promote its
Case(...) into cases.py's build_cases() and it becomes part of the real
suite.

Usage: uv run python -m services.chatbot_svc.evals.run_multiturn
Env:   same as run.py (DATABASE_URL, GROQ_API_KEY, CHATBOT_RR_URL,
       INTERNAL_API_TOKEN, EVAL_SHOP_DOMAIN, CHATBOT_MODEL, CHATBOT_EVAL_MODEL)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic_evals import Dataset

from services.chatbot_svc.evals.cases_multiturn import build_multiturn_cases
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
from services.chatbot_svc.evals.report import build_report
from services.chatbot_svc.evals.runner import ChatRunOutput, run_multiturn_case
from services.common.db import get_db
from services.common.models import ChatSession

_REQUIRED_ENV = ["DATABASE_URL", "GROQ_API_KEY", "CHATBOT_RR_URL", "INTERNAL_API_TOKEN",
                 "EVAL_SHOP_DOMAIN", "CHATBOT_MODEL", "CHATBOT_EVAL_MODEL"]
REPORTS_DIR = Path(__file__).parent / "reports"
_MODEL = os.environ.get("CHATBOT_MODEL", "")


def main() -> int:
    missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        print(f"missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 1

    shop = os.environ["EVAL_SHOP_DOMAIN"]
    cases = build_multiturn_cases(shop)
    if not cases:
        print(f"need >=2 products for shop {shop} to build multi-turn cases — seed the dev store first", file=sys.stderr)
        return 1
    print(f"running {len(cases)} multi-turn cases against {shop} with {_MODEL}")

    session_id = f"eval-mt-{uuid.uuid4().hex[:12]}"
    try:
        with get_db() as s:
            now = datetime.now(timezone.utc)
            s.add(ChatSession(id=session_id, shopDomain=shop, title="multiturn eval run",
                              createdAt=now, updatedAt=now))
    except Exception as exc:  # noqa: BLE001
        print(f"could not create eval ChatSession for {shop}: {exc}", file=sys.stderr)
        return 1

    try:
        dataset = Dataset(
            name="chatbot_multiturn_eval",
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

        async def task(turns: list[str]) -> ChatRunOutput:
            return await run_multiturn_case(turns, shop, session_id)

        # serial: each case is itself multiple sequential Groq calls; running
        # cases in parallel on top of that multiplies 429 risk for no benefit
        # in a dev-loop tool.
        lib_report = dataset.evaluate_sync(task, max_concurrency=1)
        lib_report.print(include_input=True, include_output=False)
    finally:
        with get_db() as s:
            row = s.get(ChatSession, session_id)
            if row:
                s.delete(row)

    case_dicts = []
    for rc in lib_report.cases:
        out: ChatRunOutput = rc.output
        meta = rc.metadata or {}
        all_results = {**(rc.assertions or {}), **(rc.scores or {})}
        case_dicts.append({
            "case_id": rc.name,
            "prompt": rc.inputs,
            "reply": out.reply if out else "",
            "ask": out.ask if out else None,
            "expected_facts": meta.get("expected_facts", []),
            "expected_tools": meta.get("expected_tools", []),
            "actual_tools": out.tool_names() if out else [],
            "tool_errors": out.tool_errors if out else [],
            "assertions": {n: a.value for n, a in all_results.items()},
            "assertion_reasons": {n: a.reason for n, a in all_results.items()},
            "duration_s": rc.task_duration,
            "input_tokens": out.input_tokens if out else 0,
            "output_tokens": out.output_tokens if out else 0,
            "error": out.error if out else "NoOutput",
            "retries": out.retries if out else 0,
        })

    report = build_report(case_dicts, model=_MODEL)

    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (REPORTS_DIR / f"multiturn_{ts}.json").write_text(json.dumps(report, indent=2))
    (REPORTS_DIR / "multiturn_latest.json").write_text(json.dumps(report, indent=2))
    print(f"\n{report['overall_pass_rate_pct']}% overall pass rate — report written to reports/multiturn_{ts}.json")

    return 0 if report["overall_pass_rate_pct"] == 100.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
