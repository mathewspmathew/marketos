# Chatbot Evaluation Report — 2026-07-05

Model: `groq:openai/gpt-oss-120b` · 5 cases · overall **20.0%** pass

| Layer | Pass | Fail | Rate |
|---|---|---|---|
| output_correctness | 4 | 1 | 80.0% |
| structured_output | 4 | 1 | 80.0% |
| tool_selection | 3 | 2 | 60.0% |
| hallucination | 5 | 0 | 100.0% |
| business_logic | 4 | 1 | 80.0% |

p50 latency 65357ms · p95 170410ms · est. cost $0.0183/run

Failures: 0 exceptions · 1 validation errors · 0 timeouts

## Failed cases

| Case | Failed layers | Expected tools | Actual tools |
|---|---|---|---|
| price_query | output_correctness | resolve_product | resolve_product, ask_user |
| dp_status | tool_selection | resolve_product, get_dynamic_pricing_status | resolve_product, ask_user |
| toggle_enable | tool_selection, business_logic | resolve_product, preview_dynamic_pricing_toggle | resolve_product, ask_user |
| nonexistent_product | structured_output | resolve_product | resolve_product, resolve_product, get_variant, resolve_product, resolve_product |
