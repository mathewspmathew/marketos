# MarketOS — Project Snapshot

A layer-by-layer view of the current system, detailed enough that someone
could rebuild the shape of it from this document alone. For package/tool
versions see [`PROJECT_TECH.md`](PROJECT_TECH.md); for a plain-English
description of what it does see [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md).

---

## Core idea

A Shopify merchant installs the app and switches **dynamic pricing** on for
the products they want to compete on. From there the system runs an
end-to-end loop:

1. **Discover** competitor listings for each dynamic-pricing-enabled product
   (Serper / Google SERP) → `CompetitorCandidate`; the merchant accepts the
   genuine matches, which become tracked `ProductUrl`s.
2. **Scrape** each accepted competitor page on a schedule (Firecrawl).
3. **Extract** structured data — title, variants, prices, images — via Groq LLM.
4. **Persist** raw markdown + images to GCS, structured data to PostgreSQL.
5. **Semantic-summarize** every variant (merchant + competitor) via Groq into
   a single text blob.
6. **Embed** that text + first image into 768D vectors using Vertex AI,
   stored in pgvector.
7. **Match** competitor variants/products against merchant variants/products
   via per-domain HNSW similarity + hybrid thresholds → `ProductMatch`
   (variant↔variant) and `ProductLevelMatch` (product↔product).
8. **Measure the market** — roll matched competitor prices into per-variant
   stats (`CompetitorPriceObservation`, `VariantCompetitorStats`).
9. **Decide and apply a price** per product from the competitor reference,
   gated by match confidence → `PriceDecision`, auto-applied to Shopify when
   eligible. The merchant can revert any applied decision.

A Pydantic-AI **chat assistant** sits alongside the UI and can answer
questions over this data and take guarded actions (toggle dynamic pricing,
apply or revert a price) behind a preview/confirm step.

---

## Layer 1 — Frontend (`shopify_ui/`)

React Router 7 app embedded in Shopify via `@shopify/shopify-app-react-router`.
Routes live in `shopify_ui/app/routes/`:

```
app.jsx / app._index.jsx        shell / dashboard
app.products.jsx                toggle dynamicPricingEnabled per product
app.matches.jsx / .lazy.jsx / .stream.jsx   review ProductMatch / ProductLevelMatch rows
app.stats._index.jsx / app.stats.$productId.jsx / .stream.jsx
                                 competitor stats, price history, revert
app.history.$id.jsx             competitor price history detail
app.settings.jsx                per-shop settings
app.apply-preview.jsx           proxy that injects INTERNAL_API_TOKEN server-side
                                 for chat-driven apply/revert actions
api.chat.jsx / api.sessions.jsx / api.sessions.$id.jsx   chat assistant (SSE → chatbot_svc)
internal.apply-chat-price.jsx   accepts chat-driven price apply/revert
internal.apply-price.jsx        direct apply-price endpoint
internal.notify-*.jsx           notification endpoints
webhooks.products.{create,update,delete}.jsx
webhooks.orders.create.jsx
webhooks.app.{scopes_update,uninstalled}.jsx
webhooks.compliance.jsx
auth.login/ , auth.$.jsx        OAuth
privacy.jsx
```

Talks to PostgreSQL directly via the Prisma JS client (session/shop/product
reads+writes), and to `services/api_gateway` over HTTP for anything that
needs to fan out to Celery.

---

## Layer 2 — API Gateway (`services/api_gateway/`)

FastAPI app, port 8000. Internal-only endpoints behind an `X-Internal-Token`
check, used by the frontend to trigger Celery tasks and to run a few
synchronous operations directly (not queued):

- `POST /internal/shopify/product-updated` — webhook relay → `shopify_sync.handle_product_update`
- `POST /internal/shopify/sync` / `/refresh` — full product resync
- price revert — calls `services/pricing_svc/revert.py::revert_price_decision` directly (plain function, not a Celery task)
- match review helpers — calls `services/pricing_svc/match_review.py` directly

---

## Layer 3 — Message broker (Redis)

Celery broker + result backend + distributed shop-locks. Queues:
`scraping_queue`, `extraction_queue`, `semantic_queue`,
`shopify_semantic_queue`, `discovery_queue`, `embedding_queue`,
`match_queue`, `stats_queue`, `pricing_queue`, `shopify_sync_queue`,
`scheduler_queue`. Routing table lives in `services/common/celery_app.py`.

---

## Layer 4 — Scheduler (`scheduler-worker`, `scheduler_queue`)

`services/scraper_svc/celery_beat.py::check_idle_configs`, fired every 30s
by celery-beat:

- `_tick_queued_discovery_jobs` → dispatches `discovery.search_products`
  for merchant-driven discovery requests queued from the UI.
- `_tick_product_urls` → dispatches `scraper.rescrape_url` for `ProductUrl`
  rows due for a rescrape (domain-spaced countdown).
- `_shopify_semantic_backfill` → catches `ShopifyVariant`s missing
  `semanticText` (e.g. dropped webhooks).

Matching and pricing are event-driven, not swept by beat.

---

## Layer 5 — Discovery (`discovery-worker`, `discovery_queue`)

`services/discovery_svc/main.py` — `@task discovery.search_products`.
Serper (Google SERP) → competitor product links → UPSERT
`CompetitorCandidate`. Merchant accepts a candidate in the UI →
`ProductUrl` created → picked up by the scraper.

---

## Layer 6 — Scraping + extraction (`scraper-worker`, `extraction-worker`)

`services/scraper_svc/`:

- `scraper.py` — `@task scraper.scrape_candidate` / `rescrape_url` /
  `scrape_listing` / `rescrape_product` (`scraping_queue`). Firecrawl →
  markdown + image URLs → saved to GCS (`services/common/gcs_utils.py`) →
  upsert `ScrapedProduct` / `ProductUrl` → hands off to extraction.
- `extractor.py` / `candidate.py` — `@task scraper.extract_candidate` /
  `extract_product` / `rescrape_extract` / `expand_listing`
  (`extraction_queue`, concurrency=1 for Groq rate limits). Reads GCS
  markdown, Groq LLM → structured JSON → upsert `ScrapedVariant` rows →
  hands off to semantic generation.

---

## Layer 7 — Semantic text (`services/scraper_svc/semantics.py`)

- `@task scraper.generate_variant_semantics` (`semantic_queue`) — competitor
  side, Groq → one `semanticText` per `ScrapedVariant`.
- `@task scraper.generate_shopify_variant_semantics` (`shopify_semantic_queue`)
  — merchant side, triggered from product webhooks via api_gateway; also
  generates product-level search queries for `ShopifyProduct`.

Both hand off to embedding.

---

## Layer 8 — Embeddings (`embedding-worker`, `embedding_queue`)

`services/embedding_svc/main.py`:

- `@task embedder.generate_embeddings` — competitor side.
- `@task shopify_embedder.generate_shopify_embeddings` — merchant side.

Reads `semanticText` + image URL, Vertex `text-embedding-004` (text, 768D)
+ `multimodalembedding@001` (image, 768D), raw-SQL `INSERT` into
`ProductEmbedding` / `ShopifyEmbedding` (pgvector columns). Competitor-side
writes hand off to the matcher.

---

## Layer 9 — Matching (`matcher-worker`, `match_queue`)

`services/matcher_svc/main.py`, `threshold.py` — `@task
matcher.match_for_scraped_product`. Per-domain HNSW similarity + hybrid
threshold per competitor domain → UPSERT `ProductMatch` (variant↔variant)
and `ProductLevelMatch` (product↔product, `MatchConfidenceTier`). A Redis
shop-lock prevents overlapping runs. Hands off to stats.

---

## Layer 10 — Stats + pricing (`pricing-worker`)

`services/pricing_svc/`:

- `stats.py` — `@task stats.recompute_for_variant` /
  `recompute_after_observation` (`stats_queue`). Rolls matched competitor
  prices into `CompetitorPriceObservation` + `VariantCompetitorStats`
  (min/median/max, elasticity inputs). Hands off to `pricing_queue`.
- `decide.py` / `main.py` — `@task pricing.decide_for_product`
  (`pricing_queue`). Decides a price from the competitor reference and the
  product's `PricingTier`, gated by match confidence (the **LIKELY-gate**:
  a `ProductLevelMatch` only counts if `confidenceTier = CONFIRMED`, or
  `confidenceTier = LIKELY` and the merchant explicitly confirmed it via
  the Matches UI — `WEAK`/`REJECTED` never count). Writes `PriceDecision`
  and, when eligible, enqueues `pricing.apply_price`.
- `apply.py` — `@task pricing.apply_price` (`pricing_queue`). Pushes the
  new price to Shopify via Admin GraphQL `productVariantsBulkUpdate`
  (Token Exchange).
- `revert.py` — plain function (not a Celery task), called synchronously
  from `services/api_gateway` to undo a `PriceDecision`.
- `match_review.py`, `product_stats.py` — synchronous helpers backing the
  Matches/Stats UI, called from `api_gateway`.

---

## Layer 11 — Shopify sync (`shopify_svc`, `shopify_sync_queue`)

`services/shopify_svc/main.py` — `@task shopify_sync.pull_products` /
`handle_product_update`. Keeps `ShopifyProduct`/`ShopifyVariant` in sync
with the live store and rebuilds sales aggregates used as pricing inputs.

---

## Layer 12 — Chat assistant (`chatbot_svc`)

FastAPI + SSE app, port 8088, Pydantic-AI agent (`agent.py`, `tools/`,
`context.py`, `sessions.py`). Answers questions over live DB data and can
call tools that write `ChatPreview` rows for apply/revert actions; the
frontend (`app.apply-preview.jsx` → `internal.apply-chat-price.jsx`) turns
a confirmed preview into a real price apply or revert.

---

## Layer 13 — Shared code (`services/common/`)

Celery app + queue routing (`celery_app.py`), SQLAlchemy engine/session
(`db.py`), ORM models mirroring the Prisma schema (`models.py`), GCS
helpers (`gcs_utils.py`), Pydantic schemas (`schemas.py`), Groq access via
`litellm.Router` (`groq_client.py`), Shopify Admin API helpers
(`shopify_client.py`, `shopify_auth.py`).

---

## Layer 14 — Data (PostgreSQL + pgvector)

Schema at `shopify_ui/prisma/schema.prisma`, shared by the JS Prisma
client and Python `prisma-client-py`.

| Stage                    | Table / Store                                              |
|---------------------------|-------------------------------------------------------------|
| Per-shop config           | `ShopSettings`, `ScrapingConfig`                            |
| Discovery                 | `DiscoveryJob`, `CompetitorCandidate`                        |
| Tracked competitor URLs   | `ProductUrl` (`UrlStatus`, `ScrapeStatus`)                   |
| Scrape raw                | GCS markdown bucket, GCS image bucket                        |
| Scrape structured         | `ScrapedProduct`, `ScrapedVariant`                            |
| Shopify mirror            | `ShopifyProduct`, `ShopifyVariant`                            |
| Semantic text             | `semanticText` column on both variant tables                 |
| Vectors (768D)            | `ProductEmbedding`, `ShopifyEmbedding` (pgvector, HNSW idx)   |
| Matches                   | `ProductMatch` (variant↔variant), `ProductLevelMatch` (product↔product, `MatchConfidenceTier`, `MatchReviewStatus`) |
| Price inputs              | `CompetitorPriceObservation`, `VariantCompetitorStats`        |
| Price decisions           | `PriceDecision` (`PricingTier`)                               |
| Chat                      | `ChatSession`, `ChatMessage`, `ChatPreview` (`ChatRole`, `PreviewKind`) |
| Sessions                  | `Session` (Shopify OAuth)                                     |

There is no `ProductSuggestion` model — suggestion/approval UX lives in the
Matches (`ProductMatch`/`ProductLevelMatch`) and chat (`ChatPreview`) flows
instead.

---

## Queue / task map

| Queue                    | Task                                                        | File                                   |
|---------------------------|--------------------------------------------------------------|------------------------------------------|
| `scheduler_queue`         | `services.scraper_svc.celery_beat.check_idle_configs`        | `services/scraper_svc/celery_beat.py`    |
| `discovery_queue`         | `discovery.search_products`                                   | `services/discovery_svc/main.py`         |
| `scraping_queue`          | `scraper.scrape_listing` / `rescrape_product` / `scrape_candidate` / `rescrape_url` | `services/scraper_svc/scraper.py` |
| `extraction_queue`        | `scraper.extract_product` / `rescrape_extract` / `extract_candidate` / `expand_listing` | `services/scraper_svc/extractor.py`, `candidate.py` |
| `semantic_queue`          | `scraper.generate_variant_semantics`                           | `services/scraper_svc/semantics.py`      |
| `shopify_semantic_queue`  | `scraper.generate_shopify_variant_semantics`                   | `services/scraper_svc/semantics.py`      |
| `embedding_queue`         | `embedder.generate_embeddings` / `shopify_embedder.generate_shopify_embeddings` | `services/embedding_svc/main.py` |
| `match_queue`             | `matcher.match_for_scraped_product`                            | `services/matcher_svc/main.py`           |
| `stats_queue`             | `stats.recompute_for_variant` / `recompute_after_observation`  | `services/pricing_svc/stats.py`          |
| `pricing_queue`           | `pricing.decide_for_product` / `apply_price`                   | `services/pricing_svc/main.py`, `apply.py` |
| `shopify_sync_queue`      | `shopify_sync.pull_products` / `handle_product_update`         | `services/shopify_svc/main.py`           |

Queue routing and the beat schedule are defined in
`services/common/celery_app.py`. Docker containers per worker are declared
in `docker-compose.yml`. On the dev laptop a single `pricing-worker`
consumes `stats_queue`, `pricing_queue`, and `shopify_sync_queue` together;
split into separate workers in production for independent scaling and
rate-limit isolation. There is no `writer_queue` — price write-back runs as
`pricing.apply_price` on `pricing_queue`.

---

## Run

```bash
Frontend: cd shopify_ui && npm run dev
Backend : docker-compose up          # redis + all workers + beat + api_gateway + chatbot_svc
Schema  : cd shopify_ui && npm run setup    # prisma generate + migrate
```
