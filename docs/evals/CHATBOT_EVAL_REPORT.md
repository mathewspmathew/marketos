# Chatbot Evaluation Report — 2026-07-06

Model: `groq:openai/gpt-oss-120b` · 5 cases · overall **40.0%** pass

| Layer | Pass | Fail | Rate |
|---|---|---|---|
| output_correctness | 5 | 0 | 100.0% |
| structured_output | 3 | 2 | 60.0% |
| tool_selection | 2 | 3 | 40.0% |
| price_hallucination | 5 | 0 | 100.0% |
| business_logic | 3 | 2 | 60.0% |

p50 latency 43770ms · p95 102410ms · tokens 26579/559 in/out

Failures: 2 exceptions · 0 validation errors · 0 timeouts

## Failed cases

| Case | Failed layers | Expected tools | Actual tools |
|---|---|---|---|
| toggle_enable | tool_selection, business_logic | resolve_product, preview_dynamic_pricing_toggle | resolve_product, get_dynamic_pricing_status, ask_user |
| nonexistent_product | structured_output, tool_selection | resolve_product |  |
| ambiguous_reference | structured_output, tool_selection, business_logic | resolve_product |  |
