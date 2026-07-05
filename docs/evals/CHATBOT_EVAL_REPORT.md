# Chatbot Evaluation Report — 2026-07-05

Model: `groq:openai/gpt-oss-120b` · 12 cases · overall **25.0%** pass

| Layer | Pass | Fail | Rate |
|---|---|---|---|
| output_correctness | 7 | 5 | 58.3% |
| structured_output | 3 | 9 | 25.0% |
| tool_selection | 3 | 9 | 25.0% |
| hallucination | 12 | 0 | 100.0% |
| business_logic | 9 | 3 | 75.0% |

LLM-judge avg: **0.0** (0-1) · p50 latency 42161ms · p95 69840ms · est. cost $0.0159/run

Failures: 9 exceptions · 0 validation errors · 0 timeouts

## Failed cases

| Case | Failed layers | Expected tools | Actual tools |
|---|---|---|---|
| price_query_camlin_663538 | output_correctness, structured_output, tool_selection | resolve_product |  |
| price_query_driftwood_&_co_883442 | output_correctness, structured_output, tool_selection | resolve_product |  |
| price_query_casio_056114 | output_correctness, structured_output, tool_selection | resolve_product |  |
| price_query_apex_athletic_228402 | output_correctness, structured_output, tool_selection | resolve_product |  |
| price_query_northcoast_basics_097330 | output_correctness, structured_output, tool_selection | resolve_product |  |
| dp_status_query | structured_output, tool_selection | resolve_product, get_dynamic_pricing_status |  |
| toggle_enable_first | structured_output, tool_selection, business_logic | resolve_product, preview_dynamic_pricing_toggle |  |
| price_increase_preview | structured_output, tool_selection, business_logic | resolve_product, preview_price_change |  |
| ambiguous_reference | structured_output, tool_selection, business_logic | resolve_product |  |
