# MarketOS — Project Snapshot

---

## Core Idea (What's Built So Far)

A Shopify merchant installs the app, configures competitor URLs, and the system runs an end-to-end loop:

1. **Scrape** competitor product pages on a schedule (Firecrawl)
2. **Extract** structured data — title, variants, prices, images — via Groq LLM
3. **Persist** raw markdown + images to GCS, structured data to PostgreSQL
4. **Semantic-summarize** every variant (merchant + competitor) via Groq into a single text blob
5. **Embed** that text + first image into 768D vectors using Vertex AI, stored in pgvector
6. **Match** each merchant variant against competitor variants via per-domain HNSW similarity + hybrid thresholds → `ProductMatch`
7. **Suggest** new title / description / price per merchant product, aggregated from matched competitors (Groq for copy, statistics for price) → `ProductSuggestion` + `VariantPriceSuggestion`
8. **Apply** — merchant reviews and approves in the Shopify UI; approved values are written back to Shopify via Admin API

---

## User Flow Diagram (with files + dispatch path)

> Visual version: [`docs/marketos_flow.png`](docs/marketos_flow.png) — regenerate via `uv run python scripts/generate_flow_diagram.py`.

![MarketOS data flow](docs/marketos_flow.png)

```
┌───────────────────────────────────────────────────────────────────────────┐
│                       MERCHANT (Shopify Admin)                            │
└───────────────────────────────────────────────────────────────────────────┘
        │
        │  OAuth install
        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  Shopify UI  —  shopify_ui/app/routes/                                    │
│    • app.jsx                  shell / nav                                 │
│    • app._index.jsx           dashboard                                   │
│    • app.controller.jsx       create / edit / delete ScrapingConfig       │
│    • app.matches.jsx          view ProductMatch rows                      │
│    • app.suggestions.jsx      review + Apply ProductSuggestion            │
│    • webhooks.products.{create,update,delete}.jsx                         │
└───────────────────────────────────────────────────────────────────────────┘
        │                                              │
        │ merchant saves ScrapingConfig                │ Shopify fires product webhook
        │ (Prisma JS client)                           │ (create / update)
        ▼                                              ▼
┌──────────────────────────────┐         ┌───────────────────────────────────┐
│  PostgreSQL (Aiven)          │         │  services/api_gateway/main.py     │
│  + pgvector                  │         │  FastAPI · port 8000              │
│  shared via Prisma schema    │◀────────│  POST /internal/shopify/          │
│  prisma/schema.prisma        │  reads  │       product-updated             │
└──────────────────────────────┘         │  POST /internal/suggestion/       │
        ▲                                │       regenerate[-product]        │
        │                                └───────────────────────────────────┘
        │                                              │
        │                                              │ celery_app.send_task(...)
        │                                              ▼
        │              ┌───────────────────────────────────────────────────┐
        │              │  Redis broker (REDIS_URL)                         │
        │              │  queues: scraping_queue, extraction_queue,        │
        │              │          semantic_queue, embedding_queue,         │
        │              │          match_queue, suggestion_queue,           │
        │              │          scheduler_queue                          │
        │              └───────────────────────────────────────────────────┘
        │                                              ▲
        │ celery-beat fires every 30s                  │ send_task
        │ (services/common/celery_app.py beat_schedule)│
        ▼                                              │
┌───────────────────────────────────────────────────────────────────────────┐
│  scheduler-worker  —  services/scraper_svc/celery_beat.py                 │
│    • check_idle_configs        (every 30s)                                │
│        picks IDLE ScrapingConfig rows whose nextScrapAt has passed,       │
│        sets status=QUEUED, then send_task →                               │
│          scraper.scrape_listing        (new configs)                      │
│          scraper.rescrape_product      (per-product refresh,              │
│                                         domain-spaced countdown)          │
│    • _shopify_semantic_backfill                                           │
│        catches ShopifyVariants missing semanticText (dropped webhooks)    │
│    NOTE: matcher is fully event-driven — no periodic sweep                │
└───────────────────────────────────────────────────────────────────────────┘
        │                                                       │
        │ scraping_queue                                        │ match_queue
        ▼                                                       ▼
┌─────────────────────────────────────────────┐    (see matcher block below)
│  scraper-worker                             │
│  services/scraper_svc/scraper.py            │
│    @task scraper.scrape_listing             │
│    @task scraper.rescrape_product           │
│  helpers: services/scraper_svc/helpers.py   │
│  gcs:     services/common/gcs_utils.py      │
│                                             │
│  1. Firecrawl API   → markdown + img URLs   │
│  2. Save markdown   → GCS (markdown bucket) │
│  3. Save images     → GCS (image bucket)    │
│  4. Upsert ScrapedProduct / ProductUrl      │
│  5. send_task(scraper.extract_product, ...) │
└─────────────────────────────────────────────┘
        │
        │ extraction_queue
        ▼
┌─────────────────────────────────────────────┐
│  extraction-worker                          │
│  services/scraper_svc/extractor.py          │
│    @task scraper.extract_product            │
│    @task scraper.rescrape_extract           │
│  (concurrency=1, rate_limit=3/m for Groq)   │
│                                             │
│  1. Read GCS markdown                       │
│  2. Groq LLM → structured JSON              │
│     (title, variants, prices, specs)        │
│  3. Upsert ScrapedVariant rows              │
│  4. set_next_scrap_at / mark_task_done      │
│  5. send_task(                              │
│       scraper.generate_variant_semantics)   │
└─────────────────────────────────────────────┘
        │
        │ semantic_queue
        ▼
┌─────────────────────────────────────────────┐         ┌──────────────────────────────────────┐
│  semantic-worker                            │◀────────│  Shopify-side variants (parallel)    │
│  services/scraper_svc/semantics.py          │         │                                      │
│    @task scraper.generate_variant_semantics │         │  webhooks.products.create.jsx        │
│      (competitor variants)                  │         │  webhooks.products.update.jsx        │
│    @task scraper.generate_shopify_variant_  │  POST   │     ↓ Prisma upsert                  │
│            semantics                        │◀────────│  ShopifyProduct / ShopifyVariant     │
│      (merchant's own variants)              │ /internal│     ↓ fetch                          │
│                                             │ /shopify│  services/api_gateway/main.py        │
│  Groq → one semanticText per variant        │ /product│     ↓ send_task                      │
│  (concurrency=1, rate_limit=3/m)            │ -updated│  scraper.generate_shopify_variant_   │
│                                             │         │       semantics                      │
│  send_task →                                │         └──────────────────────────────────────┘
│    embedder.generate_embeddings  (scraped)  │
│    shopify_embedder.generate_shopify_       │
│      embeddings (merchant)                  │
└─────────────────────────────────────────────┘
        │
        │ embedding_queue
        ▼
┌─────────────────────────────────────────────┐
│  embedding-worker                           │
│  services/embedding_svc/main.py             │
│    @task embedder.generate_embeddings       │
│    @task shopify_embedder.generate_shopify_ │
│            embeddings                       │
│  (rate_limit=10/m on Vertex)                │
│                                             │
│  1. Read semanticText + image URL from DB   │
│  2. Vertex text-embedding (768D)            │
│  3. Vertex multimodal image embedding (768D)│
│  4. Raw SQL INSERT → ProductEmbedding /     │
│     ShopifyEmbedding (pgvector columns)     │
└─────────────────────────────────────────────┘
        │
        │ Event-driven matcher trigger from embedding tail:
        │   • Competitor PE written →                                │
        │     send_task matcher.match_for_shop(shop_domain, full=False)
        │   • Shopify SE written →                                   │
        │     send_task matcher.match_for_variant(shop_domain, vid)
        │ match_queue
        ▼
┌─────────────────────────────────────────────┐
│  matcher-worker                             │
│  services/matcher_svc/main.py               │
│  services/matcher_svc/threshold.py          │
│    @task matcher.match_for_shop             │
│    @task matcher.match_for_variant          │
│                                             │
│  Dirty-flag selector (full=False):          │
│    • PE.matchedAt < PE.vectorizedAt OR NULL │
│      → re-match every variant in shop       │
│    • else SE.matchedAt < SE.updatedAt       │
│      → only those merchant variants         │
│  (full=True kept as manual escape hatch     │
│   for threshold/algo changes)               │
│                                             │
│  1. Per-domain HNSW similarity              │
│     (ef_search=100, limit=50)               │
│  2. Hybrid threshold per competitor domain  │
│  3. UPSERT ProductMatch (matchScore)        │
│  4. Stamp SE.matchedAt = NOW() (per variant)│
│  5. Stamp PE.matchedAt = NOW() (per shop,   │
│     after fan-out — optimistic)             │
│  Redis shop-lock prevents overlap (30 min)  │
└─────────────────────────────────────────────┘
        │
        │ suggestion_queue
        │ (triggered from UI "Re-suggest" via api_gateway)
        ▼
┌─────────────────────────────────────────────┐
│  suggestion-worker                          │
│  services/suggestion_svc/main.py            │
│    @task suggestion.suggest_for_shop        │
│    @task suggestion.suggest_for_product     │
│  (concurrency=1, rate_limit=3/m on Groq)    │
│                                             │
│  1. Pull merchant variants + matched        │
│     ScrapedVariants (matchScore ≥ 65)       │
│  2. Filter INR + IQR outliers               │
│  3. Per-variant min/median/max →            │
│     UPSERT VariantPriceSuggestion           │
│  4. Aggregate competitor titles/descs →     │
│     Groq → UPSERT ProductSuggestion         │
│     (suggestedTitle, suggestedDescription,  │
│      rationale)                             │
│     Preserves edited* / chosenPrice fields  │
└─────────────────────────────────────────────┘
        │
        │ merchant opens
        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  shopify_ui/app/routes/app.suggestions.jsx                                │
│    • shows VariantPriceSuggestion + ProductSuggestion                     │
│    • merchant edits or Approves                                           │
│    • on Apply: route writes back to Shopify Admin API                     │
│      using the merchant's session token                                   │
└───────────────────────────────────────────────────────────────────────────┘
```

### Data stored at each stage

| Stage                | Table / Store                                  |
|----------------------|------------------------------------------------|
| Config               | `ScrapingConfig`, `ProductUrl`                 |
| Scrape raw           | GCS markdown bucket, GCS image bucket          |
| Scrape structured    | `ScrapedProduct`, `ScrapedVariant`             |
| Shopify mirror       | `ShopifyProduct`, `ShopifyVariant`             |
| Semantic text        | `semanticText` column on both variant tables   |
| Vectors (768D)       | `ProductEmbedding`, `ShopifyEmbedding` (pgvector, HNSW idx; `matchedAt` tracks last consumed by matcher) |
| Matches              | `ProductMatch` (shopify_variant ↔ scraped_variant, score)    |
| Suggestions          | `ProductSuggestion`, `VariantPriceSuggestion`  |
| Sessions             | `Session` (Shopify OAuth)                      |

---

## Celery Task / Queue Map

| Queue              | Task                                              | File                                  |
|--------------------|---------------------------------------------------|---------------------------------------|
| `scheduler_queue`  | `services.scraper_svc.celery_beat.check_idle_configs` | `services/scraper_svc/celery_beat.py` |
| `scraping_queue`   | `scraper.scrape_listing`                          | `services/scraper_svc/scraper.py`     |
| `scraping_queue`   | `scraper.rescrape_product`                        | `services/scraper_svc/scraper.py`     |
| `extraction_queue` | `scraper.extract_product`                         | `services/scraper_svc/extractor.py`   |
| `extraction_queue` | `scraper.rescrape_extract`                        | `services/scraper_svc/extractor.py`   |
| `semantic_queue`   | `scraper.generate_variant_semantics`              | `services/scraper_svc/semantics.py`   |
| `semantic_queue`   | `scraper.generate_shopify_variant_semantics`      | `services/scraper_svc/semantics.py`   |
| `embedding_queue`  | `embedder.generate_embeddings`                    | `services/embedding_svc/main.py`      |
| `embedding_queue`  | `shopify_embedder.generate_shopify_embeddings`    | `services/embedding_svc/main.py`      |
| `match_queue`      | `matcher.match_for_shop`                          | `services/matcher_svc/main.py`        |
| `match_queue`      | `matcher.match_for_variant`                       | `services/matcher_svc/main.py`        |
| `suggestion_queue` | `suggestion.suggest_for_shop`                     | `services/suggestion_svc/main.py`     |
| `suggestion_queue` | `suggestion.suggest_for_product`                  | `services/suggestion_svc/main.py`     |

Queue routing and beat schedule are defined in `services/common/celery_app.py`. Docker containers per queue are declared in `docker-compose.yml`.

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
| SQLAlchemy | 2.0.49 |
| FastAPI | 0.136.1 |

### Infrastructure
| Component | Details |
|-----------|---------|
| Database | PostgreSQL + pgvector extension (768D vectors, HNSW idx) |
| ORM | Prisma (shared schema, JS + Python clients) + SQLAlchemy (raw pgvector + matcher reads) |
| Queue broker | Redis (also used for shop locks) |
| Embeddings | Vertex AI (text + multimodal image, 768D) |
| Scraping | Firecrawl API |
| LLM (extract / semantics / copy) | Groq (llama-3.1-8b-instant) |
| Object storage | Google Cloud Storage (markdown + image buckets) |
| Internal API | FastAPI (`services/api_gateway/main.py`, port 8000, Docker-network only) |

---
