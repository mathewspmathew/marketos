"""Aggregate per-case eval results into the report JSON and markdown."""
from __future__ import annotations

from datetime import datetime, timezone

# Boolean layers — pass/fail per case
BOOL_LAYERS = [
    "output_correctness",
    "structured_output",
    "tool_selection",
    "price_hallucination",
    "business_logic",
    "tool_success",
]

# Numeric layers — averaged across cases (0–1 score)
SCORE_LAYERS = [
    "tool_recall",
    "tool_precision",
]

ALL_LAYERS = BOOL_LAYERS + SCORE_LAYERS


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(int(len(values) * pct), len(values) - 1)
    return values[idx]


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def build_report(
    cases: list[dict],
    *,
    model: str,
) -> dict:
    total = len(cases)

    # --- boolean layers ---
    bool_layers: dict[str, dict] = {}
    for layer in BOOL_LAYERS:
        passed = sum(1 for c in cases if c["assertions"].get(layer, False))
        bool_layers[layer] = {
            "pass": passed,
            "fail": total - passed,
            "rate_pct": _pct(passed, total),
        }

    # --- score layers (numeric 0–1) ---
    score_layers: dict[str, dict] = {}
    for layer in SCORE_LAYERS:
        scores = [
            float(c["assertions"].get(layer, 0.0))
            for c in cases
            if layer in c["assertions"]
        ]
        score_layers[layer] = {
            "avg": _avg(scores),
            "min": round(min(scores), 3) if scores else 0.0,
            "max": round(max(scores), 3) if scores else 0.0,
        }

    # --- overall pass: all BOOL layers green (score layers are informational) ---
    overall_pass = sum(
        1 for c in cases if all(c["assertions"].get(l, False) for l in BOOL_LAYERS)
    )

    # --- latency ---
    durations_ms = [c["duration_s"] * 1000 for c in cases]

    # --- token totals ---
    in_tok = sum(c["input_tokens"] for c in cases)
    out_tok = sum(c["output_tokens"] for c in cases)

    # --- error rate ---
    error_cases = sum(
        1 for c in cases if c.get("error") or c.get("retries", 0) > 0
    )
    error_rate_pct = _pct(error_cases, total)

    # --- failure breakdown ---
    failures = {"exceptions": 0, "validation_errors": 0, "timeouts": 0}
    for c in cases:
        err = c.get("error")
        if err is None:
            if c.get("retries", 0) > 0:
                failures["validation_errors"] += 1
        elif "Timeout" in err:
            failures["timeouts"] += 1
        else:
            failures["exceptions"] += 1

    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "cases_total": total,
        "overall_pass_rate_pct": _pct(overall_pass, total),
        "layers": bool_layers,
        "score_layers": score_layers,
        "metrics": {
            "tool_recall_avg": score_layers["tool_recall"]["avg"],
            "tool_precision_avg": score_layers["tool_precision"]["avg"],
            "error_rate_pct": error_rate_pct,
            "latency_ms": {
                "p50": round(_percentile(durations_ms, 0.50)),
                "p95": round(_percentile(durations_ms, 0.95)),
            },
        },
        # cost lives in the Logfire dashboard (it prices each LLM span itself)
        "tokens": {"input": in_tok, "output": out_tok},
        "failures": failures,
        "case_results": [dict(c) for c in cases],
    }


def render_markdown(report: dict) -> str:
    m = report["metrics"]
    lms = m["latency_ms"]

    lines = [
        f"# Chatbot Evaluation Report — {report['run_at'][:10]}",
        "",
        f"Model: `{report['model']}` · {report['cases_total']} cases · "
        f"overall **{report['overall_pass_rate_pct']}%** pass",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Tool Recall (avg) | {m['tool_recall_avg']:.1%} |",
        f"| Tool Precision (avg) | {m['tool_precision_avg']:.1%} |",
        f"| Error Rate | {m['error_rate_pct']}% |",
        f"| Latency P50 | {lms['p50']} ms |",
        f"| Latency P95 | {lms['p95']} ms |",
        f"| Tokens (in / out) | {report['tokens']['input']} / {report['tokens']['output']} |",
        "",
        "## Layer Results (pass / fail)",
        "",
        "| Layer | Pass | Fail | Rate |",
        "|---|---|---|---|",
    ]
    for layer, s in report["layers"].items():
        lines.append(f"| {layer} | {s['pass']} | {s['fail']} | {s['rate_pct']}% |")

    lines += [
        "",
        "## Tool Score Layers",
        "",
        "| Layer | Avg | Min | Max |",
        "|---|---|---|---|",
    ]
    for layer, s in report["score_layers"].items():
        lines.append(
            f"| {layer} | {s['avg']:.1%} | {s['min']:.1%} | {s['max']:.1%} |"
        )

    lines += [
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
        failed = [
            layer for layer in BOOL_LAYERS if not c["assertions"].get(layer, False)
        ]
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
