"""Aggregate per-case eval results into the report JSON and markdown."""
from __future__ import annotations

from datetime import datetime, timezone

LAYERS = [
    "output_correctness",
    "structured_output",
    "tool_selection",
    "hallucination",
    "business_logic",
]

# Groq pricing for llama-3.3-70b-versatile (USD per 1M tokens, 2026-06 list price)
_INPUT_USD_PER_M = 0.59
_OUTPUT_USD_PER_M = 0.79


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(int(len(values) * pct), len(values) - 1)
    return values[idx]


def build_report(cases: list[dict], *, model: str) -> dict:
    total = len(cases)
    layers = {}
    for layer in LAYERS:
        passed = sum(1 for c in cases if c["assertions"].get(layer, False))
        layers[layer] = {"pass": passed, "fail": total - passed, "rate_pct": _pct(passed, total)}

    overall_pass = sum(
        1 for c in cases if all(c["assertions"].get(l, False) for l in LAYERS)
    )
    durations_ms = [c["duration_s"] * 1000 for c in cases]
    in_tok = sum(c["input_tokens"] for c in cases)
    out_tok = sum(c["output_tokens"] for c in cases)

    failures = {"exceptions": 0, "validation_errors": 0, "timeouts": 0}
    for c in cases:
        err = c.get("error")
        if err is None:
            if c.get("retries", 0) > 0:
                failures["validation_errors"] += 1
        # substring, not a fixed set: catches TimeoutError plus httpx's
        # ConnectTimeout / ReadTimeout / WriteTimeout / PoolTimeout
        elif "Timeout" in err:
            failures["timeouts"] += 1
        else:
            failures["exceptions"] += 1

    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "cases_total": total,
        "overall_pass_rate_pct": _pct(overall_pass, total),
        "layers": layers,
        "latency_ms": {
            "p50": round(_percentile(durations_ms, 0.50)),
            "p95": round(_percentile(durations_ms, 0.95)),
        },
        "tokens": {
            "input": in_tok,
            "output": out_tok,
            "est_cost_usd": round(
                in_tok / 1e6 * _INPUT_USD_PER_M + out_tok / 1e6 * _OUTPUT_USD_PER_M, 4
            ),
        },
        "failures": failures,
        "case_results": [dict(c) for c in cases],
    }


def render_markdown(report: dict) -> str:
    lines = [
        f"# Chatbot Evaluation Report — {report['run_at'][:10]}",
        "",
        f"Model: `{report['model']}` · {report['cases_total']} cases · "
        f"overall **{report['overall_pass_rate_pct']}%** pass",
        "",
        "| Layer | Pass | Fail | Rate |",
        "|---|---|---|---|",
    ]
    for layer, s in report["layers"].items():
        lines.append(f"| {layer} | {s['pass']} | {s['fail']} | {s['rate_pct']}% |")
    lines += [
        "",
        f"p50 latency {report['latency_ms']['p50']}ms · "
        f"p95 {report['latency_ms']['p95']}ms · "
        f"est. cost ${report['tokens']['est_cost_usd']}/run",
        "",
        f"Failures: {report['failures']['exceptions']} exceptions · "
        f"{report['failures']['validation_errors']} validation errors · "
        f"{report['failures']['timeouts']} timeouts",
        "",
        "## Failed cases",
        "",
        "| Case | Failed layers | Expected tools | Actual tools |",
        "|---|---|---|---|",
    ]
    any_failed = False
    for c in report["case_results"]:
        failed = [layer for layer in LAYERS if not c["assertions"].get(layer, False)]
        if failed:
            any_failed = True
            lines.append(
                f"| {c['case_id']} | {', '.join(failed)} | "
                f"{', '.join(c['expected_tools'])} | {', '.join(c['actual_tools'])} |"
            )
    if not any_failed:
        lines.append("| — | all cases passed | | |")
    lines.append("")
    return "\n".join(lines)
