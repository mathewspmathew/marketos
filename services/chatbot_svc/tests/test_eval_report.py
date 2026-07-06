from services.chatbot_svc.evals.report import LAYERS, build_report, render_markdown


def _case(name, assertions, duration=1.0, in_tok=100, out_tok=20, error=None):
    return {
        "case_id": name,
        "prompt": f"prompt for {name}",
        "expected_tools": ["resolve_product"],
        "actual_tools": ["resolve_product"],
        "assertions": assertions,       # {layer_name: bool}
        "ask": None,
        "duration_s": duration,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "error": error,
        "retries": 0,
    }


def _all_pass():
    return {l: True for l in LAYERS}


def test_build_report_aggregates_layers_and_overall():
    cases = [_case("a", _all_pass()), _case("b", {**_all_pass(), "tool_selection": False})]
    r = build_report(cases, model="groq:test")
    assert r["cases_total"] == 2
    assert r["layers"]["tool_selection"] == {"pass": 1, "fail": 1, "rate_pct": 50.0}
    assert r["layers"]["output_correctness"]["rate_pct"] == 100.0
    assert r["overall_pass_rate_pct"] == 50.0  # a case passes overall only if all layers pass
    assert r["model"] == "groq:test"


def test_build_report_cross_cutting_metrics():
    cases = [
        _case("a", _all_pass(), duration=1.0, in_tok=100, out_tok=10),
        _case("b", _all_pass(), duration=3.0, in_tok=300, out_tok=30, error="TimeoutError"),
    ]
    r = build_report(cases, model="m")
    assert r["tokens"]["input"] == 400 and r["tokens"]["output"] == 40
    assert r["tokens"]["est_cost_usd"] > 0
    assert r["latency_ms"]["p50"] <= r["latency_ms"]["p95"]
    assert r["failures"] == {"exceptions": 0, "validation_errors": 0, "timeouts": 1}
    assert "llm_judge" not in r


def test_render_markdown_contains_scoreboard_and_failures():
    cases = [_case("good", _all_pass()), _case("bad", {**_all_pass(), "hallucination": False})]
    md = render_markdown(build_report(cases, model="m"))
    assert "| Layer" in md and "hallucination" in md
    assert "## Failed cases" in md and "bad" in md and "good" not in md.split("## Failed cases")[1]


def test_build_report_handles_zero_cases():
    r = build_report([], model="m")
    assert r["cases_total"] == 0
    assert r["overall_pass_rate_pct"] == 0.0
    assert r["latency_ms"] == {"p50": 0, "p95": 0}
    render_markdown(r)  # must not raise
