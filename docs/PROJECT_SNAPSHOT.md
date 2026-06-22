# MarketOS — Project Snapshot

---

## Core Idea (What's Built So Far)

A Shopify merchant installs the app and switches **dynamic pricing** on for the
products they want to compete on. From there the system runs an end-to-end loop:

1. **Discover** competitor listings for each dynamic-pricing-enabled product
   (Serper / Google SERP) → `CompetitorCandidate`; the merchant accepts the
   genuine matches, which become tracked `ProductUrl`s
2. **Scrape** each accepted competitor page on a schedule (Firecrawl)
3. **Extract** structured data — title, variants, prices, images — via Groq LLM
4. **Persist** raw markdown + images to GCS, structured data to PostgreSQL
5. **Semantic-summarize** every variant (merchant + competitor) via Groq into a
   single text blob
6. **Embed** that text + first image into 768D vectors using Vertex AI, stored
   in pgvector
7. **Match** competitor variants/products against merchant variants/products via
   per-domain HNSW similarity + hybrid thresholds → `ProductMatch` (variant↔variant)
   and `ProductLevelMatch` (product↔product)
8. **Measure the market** — roll matched competitor prices into per-variant
   stats (`CompetitorPriceObservation`, `VariantCompetitorStats`)
9. **Decide a price** per variant/product from the competitor reference and the
   product's pricing tier → `PriceDecision`
    auto-apply)

A Pydantic-AI **chat assistant** sits alongside the UI and can answer questions
over this data and take guarded actions (toggle dynamic pricing, apply a price).

---

## User Flow Diagram (with files + dispatch path)

> Visual version: [`docs/ARCHITECTURE.svg`](ARCHITECTURE.svg).

```
┌───────────────────────────────────────────────────────────────────────────┐
│                       MERCHANT (Shopify Admin)                            │
└───────────────────────────────────────────────────────────────────────────┘
        │
        │  OAuth install
        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  Shopify UI  —  shopify_ui/app/routes/                                    │
│    • app.jsx / app._index.jsx     shell / dashboard                       │
│    • app.dynamic.jsx              toggle dynamicPricingEnabled per product │
│    • app.discover.$id.jsx         accept / reject CompetitorCandidate      │
│    • app.matches.jsx              view ProductMatch rows                   │
│    • app.stats.*.jsx / app.history.$id.jsx  competitor stats + history    │
│    • app.pricing.*.jsx            per-variant price history / controls     │
│    • app.approve.jsx / app.rules.jsx / app.alerts.jsx                     │
│    • app.chatbot.jsx              chat assistant (SSE → chatbot_svc)       │
│    • app.query-studio.jsx / api.query-studio.jsx  chat Query Studio       │
│    • app.additional.jsx           manage scraping configs                  │
│    • app.apply-preview.jsx        proxy for chat-driven actions           │
│    • internal.apply-chat-*.jsx    accept price/flag updates from chatbot  │
│    • webhooks.products.{create,update,delete}.jsx                         │
└───────────────────────────────────────────────────────────────────────────┘
        │                                              │
        │ merchant toggles dynamic pricing /           │ Shopify fires product webhook
        │ accepts candidates (Prisma JS client)        │ (create / update / delete)
        ▼                                              ▼
┌──────────────────────────────┐         ┌───────────────────────────────────┐
│  PostgreSQL (Aiven)          │         │  services/api_gateway/main.py     │
│  + pgvector                  │         │  FastAPI · port 8000              │
│  shared via Prisma schema    │◀────────│  POST /internal/shopify/          │
│  shopify_ui/prisma/          │  reads  │       product-updated             │
│      schema.prisma           │         │  POST /internal/shopify/sync      │
└──────────────────────────────┘         │  POST /internal/shopify/refresh   │
        ▲                                └───────────────────────────────────┘
        │                                              │
        │                                              │ celery_app.send_task(...)
        │                                              ▼
        │              ┌───────────────────────────────────────────────────┐
        │              │  Redis broker (REDIS_URL)                         │
        │              │  queues: discovery_queue, scraping_queue,         │
        │              │   extraction_queue, semantic_queue,               │
        │              │   shopify_semantic_queue, embedding_queue,        │
        │              │   match_queue, stats_queue, pricing_queue,        │
        │              │   shopify_sync_queue, writer_queue, scheduler_queue│
        │              └───────────────────────────────────────────────────┘
        │                                              ▲
        │ celery-beat fires check_idle_configs every   │ send_task
        │ 30s (services/common/celery_app.py)          │
        ▼                                              │
┌───────────────────────────────────────────────────────────────────────────┐
│  scheduler-worker  —  services/scraper_svc/celery_beat.py                 │
│    check_idle_configs (every 30s):                                        │
│    • _tick_queued_discovery_jobs       → discovery.search_products        │
│        (merchant-driven discovery requests queued from UI)                │
│    • _tick_product_urls                → scraper.rescrape_url (due URLs,   │
│                                          domain-spaced countdown)         │
│    • _shopify_semantic_backfill        → catch ShopifyVariants missing    │
│                                          semanticText (dropped webhooks)  │
│    NOTE: matcher + pricing are event-driven — no periodic sweep           │
└───────────────────────────────────────────────────────────────────────────┘
        │ discovery_queue
        ▼
┌─────────────────────────────────────────────┐
│  discovery-worker                           │
│  services/discovery_svc/main.py             │
│    @task discovery.search_products          │
│  Serper (Google SERP) → competitor product  │
│  links → UPSERT CompetitorCandidate         │
│  (merchant accepts in app.discover.$id.jsx  │
│   → ProductUrl created → scrape)            │
└─────────────────────────────────────────────┘
        │ scraping_queue
        ▼
┌─────────────────────────────────────────────┐
│  scraper-worker                             │
│  services/scraper_svc/scraper.py            │
│    @task scraper.scrape_candidate           │
│    @task scraper.rescrape_url               │
│    @task scraper.scrape_listing / rescrape_product │
│  helpers: services/scraper_svc/helpers.py   │
│  gcs:     services/common/gcs_utils.py      │
│                                             │
│  1. Firecrawl API   → markdown + img URLs   │
│  2. Save markdown   → GCS (markdown bucket) │
│  3. Save images     → GCS (image bucket)    │
│  4. Upsert ScrapedProduct / ProductUrl      │
│  5. send_task(scraper.extract_candidate…)   │
└─────────────────────────────────────────────┘
        │ extraction_queue
        ▼
┌─────────────────────────────────────────────┐
│  extraction-worker                          │
│  services/scraper_svc/extractor.py          │
│    @task scraper.extract_candidate          │
│    @task scraper.extract_product            │
│    @task scraper.rescrape_extract           │
│    @task scraper.expand_listing             │
│  (concurrency=1 for Groq rate limits)       │
│                                             │
│  1. Read GCS markdown                       │
│  2. Groq LLM → structured JSON              │
│  3. Upsert ScrapedVariant rows              │
│  4. set_next_scrap_at / mark_task_done      │
│  5. send_task(generate_variant_semantics)   │
└─────────────────────────────────────────────┘
        │ semantic_queue                          shopify_semantic_queue
        ▼                                         ▲ (merchant side, parallel)
┌─────────────────────────────────────────────┐  │
│  semantic-worker (competitor)               │  │  shopify-semantic-worker
│  shopify-semantic-worker (merchant)         │  │  webhooks.products.* → api_gateway
│  services/scraper_svc/semantics.py          │  │  → generate_shopify_variant_semantics
│    @task scraper.generate_variant_semantics │  │  (also product-level search query
│    @task scraper.generate_shopify_variant_  │◀─┘   generation for ShopifyProducts)
│            semantics                        │
│  Groq → one semanticText per variant        │
│  send_task → embedder.* / shopify_embedder.*│
└─────────────────────────────────────────────┘
        │ embedding_queue
        ▼
┌─────────────────────────────────────────────┐
│  embedding-worker                           │
│  services/embedding_svc/main.py             │
│    @task embedder.generate_embeddings       │
│    @task shopify_embedder.generate_shopify_ │
│            embeddings                       │
│                                             │
│  1. Read semanticText + image URL from DB   │
│  2. Vertex text-embedding-004 (768D)        │
│  3. Vertex multimodalembedding@001 (768D)   │
│  4. Raw SQL INSERT → ProductEmbedding /     │
│     ShopifyEmbedding (pgvector columns)     │
│  5. Competitor PE written →                 │
│     send_task matcher.match_for_scraped_product │
└─────────────────────────────────────────────┘
        │ match_queue
        ▼
┌─────────────────────────────────────────────┐
│  matcher-worker                             │
│  services/matcher_svc/main.py               │
│  services/matcher_svc/threshold.py          │
│    @task matcher.match_for_scraped_product  │
│                                             │
│  1. Per-domain HNSW similarity              │
│  2. Hybrid threshold per competitor domain  │
│  3. UPSERT ProductMatch (variant↔variant)   │
│     + ProductLevelMatch (product↔product,   │
│       MatchConfidenceTier)                  │
│  4. send_task → stats / pricing downstream  │
│  Redis shop-lock prevents overlap           │
└─────────────────────────────────────────────┘
        │ stats_queue
        ▼
┌─────────────────────────────────────────────┐
│  pricing-worker  (stats half)               │
│  services/pricing_svc/stats.py              │
│    @task stats.recompute_for_variant        │
│    @task stats.recompute_after_observation  │
│                                             │
│  Roll matched competitor prices into        │
│  CompetitorPriceObservation +               │
│  VariantCompetitorStats (min/median/max,    │
│  elasticity inputs) → send_task pricing.*   │
└─────────────────────────────────────────────┘
        │ pricing_queue
        ▼
┌─────────────────────────────────────────────┐
│  pricing-worker  (decide half)              │
│  services/pricing_svc/main.py               │
│    @task pricing.decide_for_product         │
│    @task pricing.apply_price                │
│                                             │
│  Decide per-variant / per-product price     │
│  from competitor reference + PricingTier    │
│  → UPSERT PriceDecision (v1: no auto-apply) │
└─────────────────────────────────────────────┘
│    Dashboard: shows ProductSuggestion + competitor stats; merchant approves
│    Chat: Query Studio generates ChatPreview rows; chatbot apply-price tool
│      writes internal.apply-chat-price route + UPSERT PriceDecision
│    Write-back jobs: services/shopify_svc/main.py (writer_queue):          │
│    @task shopify_writer.apply_decision (merchant-approved prices)        │
└───────────────────────────────────────────────────────────────────────────┘
```

### Data stored at each stage

| Stage                | Table / Store                                  |
|----------------------|------------------------------------------------|
| Per-shop config      | `ShopSettings`, `ScrapingConfig`               |
| Discovery            | `DiscoveryJob`, `CompetitorCandidate`          |
| Tracked competitor URLs | `ProductUrl` (`UrlStatus`, `ScrapeStatus`)  |
| Scrape raw           | GCS markdown bucket, GCS image bucket          |
| Scrape structured    | `ScrapedProduct`, `ScrapedVariant`             |
| Shopify mirror       | `ShopifyProduct`, `ShopifyVariant`             |
| Semantic text        | `semanticText` column on both variant tables   |
| Vectors (768D)       | `ProductEmbedding`, `ShopifyEmbedding` (pgvector, HNSW idx; `matchedAt` tracks last consumed by matcher) |
| Matches              | `ProductMatch` (variant↔variant), `ProductLevelMatch` (product↔product, `MatchConfidenceTier`) |
| Price inputs         | `CompetitorPriceObservation`, `VariantCompetitorStats` |
| Price decisions      | `PriceDecision` (`PricingTier`)                |
| Suggestions          | `ProductSuggestion` (`SuggestionStatus`)       |
| Chat                 | `ChatSession`, `ChatMessage`, `ChatPreview` (`ChatRole`, `PreviewKind`) |
| Sessions             | `Session` (Shopify OAuth)                      |

---

## Celery Task / Queue Map

| Queue                   | Task                                              | File                                  |
|-------------------------|---------------------------------------------------|---------------------------------------|
| `scheduler_queue`       | `services.scraper_svc.celery_beat.check_idle_configs` | `services/scraper_svc/celery_beat.py` |
| `discovery_queue`       | `discovery.search_products`                       | `services/discovery_svc/main.py`      |
| `scraping_queue`        | `scraper.scrape_listing` / `rescrape_product`     | `services/scraper_svc/scraper.py`     |
| `scraping_queue`        | `scraper.scrape_candidate` / `rescrape_url`       | `services/scraper_svc/scraper.py`     |
| `extraction_queue`      | `scraper.extract_product` / `rescrape_extract`    | `services/scraper_svc/extractor.py`   |
| `extraction_queue`      | `scraper.extract_candidate` / `expand_listing`    | `services/scraper_svc/extractor.py`   |
| `semantic_queue`        | `scraper.generate_variant_semantics`              | `services/scraper_svc/semantics.py`   |
| `shopify_semantic_queue`| `scraper.generate_shopify_variant_semantics`      | `services/scraper_svc/semantics.py`   |
| `embedding_queue`       | `embedder.generate_embeddings`                    | `services/embedding_svc/main.py`      |
| `embedding_queue`       | `shopify_embedder.generate_shopify_embeddings`    | `services/embedding_svc/main.py`      |
| `match_queue`           | `matcher.match_for_scraped_product`               | `services/matcher_svc/main.py`        |
| `stats_queue`           | `stats.recompute_for_variant` / `recompute_after_observation` | `services/pricing_svc/stats.py` |
| `pricing_queue`         | `pricing.decide_for_product` / `apply_price`      | `services/pricing_svc/main.py`        |
| `shopify_sync_queue`    | `shopify_sync.recompute_sales_aggregate` / `pull_products` | `services/shopify_svc/main.py`  |
| `writer_queue`          | `shopify_writer.apply_decision` / `sweep_pending` | `services/shopify_svc/main.py`        |

Queue routing and the beat schedule are defined in `services/common/celery_app.py`.
Docker containers per worker are declared in `docker-compose.yml`. On the dev
laptop a single `pricing-worker` consumes `stats_queue`, `pricing_queue`,
`writer_queue`, and `shopify_sync_queue` together; split into separate workers in
production for independent scaling and rate-limit isolation.

---

## Tool Versions

### Runtime
| Tool | Version |
|------|---------|
| Node.js | 20.19.1 |
| Python | 3.12.13 |
| uv (package manager) | lockfile frozen |

### Frontend (shopify_ui/)
| Package | Version |
|---------|---------|
| React | 18.3.1 |
| React Router | 7.12.0 |
| Vite | 6.3.6 |
| TypeScript | 5.9.3 |
| Prisma (JS client) | 6.16.3 |
| @shopify/shopify-app-react-router | 1.1.0 |
| @shopify/app-bridge-react | 4.2.4 |
| @shopify/cli | 3.94.3 |

### Python Services
| Package | Version |
|---------|---------|
| Celery (+ Redis) | 5.6.3 |
| Redis | 6.4.0 |
| Firecrawl-py | 4.23.0 |
| Groq | 1.2.0 |
| Google Cloud AI Platform (Vertex AI) | 1.148.1 |
| Google Cloud Storage | 3.10.1 |
| Pydantic | 2.13.3 |
| Pydantic-AI (chatbot agent) | — |
| SQLAlchemy | 2.0.49 |
| FastAPI | 0.136.1 |
| Logfire (observability) | — |

### Infrastructure
| Component | Details |
|-----------|---------|
| Database | PostgreSQL + pgvector extension (768D vectors, HNSW idx) |
| ORM | Prisma (shared schema, JS + Python clients) + SQLAlchemy (raw pgvector + matcher reads) |
| Queue broker | Redis (also used for shop locks) |
| Embeddings | Vertex AI — `text-embedding-004` (text) + `multimodalembedding@001` (image), 768D |
| Scraping | Firecrawl API |
| Discovery | Serper (Google SERP) |
| LLM (extract / semantics / copy) | Groq (`llama-3.1-8b-instant`) |
| LLM (chat assistant) | Groq (`llama-3.3-70b-versatile`) via Pydantic-AI |
| Object storage | Google Cloud Storage (markdown + image buckets) |
| Internal API | FastAPI (`services/api_gateway/main.py`, port 8000) + chatbot (`services/chatbot_svc/app.py`, port 8088) |

---
