# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MarketOS is a Shopify-embedded e-commerce intelligence platform for dynamic pricing and competitive analysis. It has two main parts:
- A React Router frontend (embedded Shopify app) in `shopify_ui/`
- Python Celery microservices for scraping, discovery, extraction, semantics, embeddings, matching, suggestion, and pricing in `services/`

Both parts share a PostgreSQL database (with pgvector) via Prisma ORM.

## Commands

### Frontend (shopify_ui/)

```bash
cd shopify_ui
npm run dev          # Start dev server with Shopify CLI tunnel
npm run build        # Production build
npm run lint         # ESLint
npm run typecheck    # TypeScript type check
npm run setup        # Generate Prisma client + run migrations
```

### Python Services

```bash
uv sync --frozen     # Install dependencies from lockfile

# Run individual workers (one Celery process per queue group)
uv run celery -A services.common.celery_app worker -Q scraping_queue       -n scraper-worker
uv run celery -A services.common.celery_app worker -Q extraction_queue     -n extraction-worker
uv run celery -A services.common.celery_app worker -Q scheduler_queue      -n scheduler-worker
uv run celery -A services.common.celery_app worker -Q discovery_queue      -n discovery-worker
uv run celery -A services.common.celery_app worker -Q semantic_queue       -n semantic-worker
uv run celery -A services.common.celery_app worker -Q shopify_semantic_queue -n shopify-semantic-worker
uv run celery -A services.common.celery_app worker -Q embedding_queue      -n embedding-worker
uv run celery -A services.common.celery_app worker -Q match_queue          -n matcher-worker
uv run celery -A services.common.celery_app worker -Q suggestion_queue,shopify_sync_queue -n suggestion-worker
uv run celery -A services.common.celery_app worker -Q stats_queue          -n elasticity-worker
uv run celery -A services.common.celery_app worker -Q pricing_queue        -n pricing-worker
uv run celery -A services.common.celery_app beat
```

### Full Stack (Docker)

```bash
docker-compose up    # Redis + all Python workers + beat scheduler + api_gateway
```

## Architecture

### Frontend (`shopify_ui/`)

React Router 7 app embedded in Shopify via `@shopify/shopify-app-react-router`. Routes live in `shopify_ui/app/routes/`. Uses Prisma JS client to store Shopify OAuth sessions and read/write product, discovery, and pricing data on the shared PostgreSQL database.

### Python Celery Workers (`services/`)

Queue routing is the source of truth in `services/common/celery_app.py`. Workers (per `docker-compose.yml`):

| Worker | Queues | Responsibility |
|--------|--------|----------------|
| scraper-worker | `scraping_queue` | Firecrawl scraping of listings, products, and discovery candidates |
| extraction-worker | `extraction_queue` | Groq LLM extraction → save to DB + GCS |
| scheduler-worker | `scheduler_queue` | Beat-driven tick: stuck-config cleanup, discovery dispatch, product-URL re-scrape, semantic backfill |
| discovery-worker | `discovery_queue` | Serper-based competitor product discovery for dynamic-pricing-enabled products |
| semantic-worker | `semantic_queue` | Variant semantic-text generation for scraped competitor variants |
| shopify-semantic-worker | `shopify_semantic_queue` | Semantic-text + product-level search-query generation for merchant's ShopifyProducts/Variants |
| embedding-worker | `embedding_queue` | Vertex AI text & image embeddings → pgvector write (both merchant and competitor sides) |
| matcher-worker | `match_queue` | Match scraped competitor variants/products to merchant variants/products via vector similarity |
| suggestion-worker | `suggestion_queue`, `shopify_sync_queue` | Generate pricing suggestions; recompute Shopify sales aggregates |
| elasticity-worker | `stats_queue` | Recompute per-variant competitor price stats and elasticity inputs |
| pricing-worker | `pricing_queue` | Decide per-variant / per-product price (v1 is suggestion-only, no auto-apply) |
| celery-beat | — | Triggers `services.scraper_svc.celery_beat.check_idle_configs` every 30s |

Shared utilities in `services/common/`: Celery app config (`celery_app.py`), Prisma Python client (`db.py`), GCS helpers (`gcs_utils.py`), Pydantic schemas (`schemas.py`).

Stubs / not yet active: `chatbot_svc/`, `guardian_svc/`, `observability_mcp/`. `api_gateway/` is built into the compose stack and exposes thin HTTP entry points.

### Database (Prisma + pgvector)

Schema at `shopify_ui/prisma/schema.prisma` — shared by both the JS Prisma client and the Python `prisma-client-py`. Key models:

- **Session**, **ShopAuth**, **ShopSettings** — Shopify OAuth + per-shop config
- **ShopifyUser** / **ShopifyProduct** / **ShopifyVariant** / **ShopifyEmbedding** — merchant store data + vectors
- **ScrapedProduct** / **ScrapedVariant** / **ProductEmbedding** — competitor product data + vectors
- **ScrapingConfig**, **ProductUrl** (`UrlStatus`, `ScrapeStatus`) — scrape job state
- **DiscoveryJob**, **CompetitorCandidate** (`DiscoveryStatus`, `CandidateStatus`) — discovery pipeline
- **ProductMatch**, **ProductLevelMatch** (`MatchConfidenceTier`) — matcher outputs
- **CompetitorPriceObservation**, **VariantCompetitorStats** — pricing inputs
- **ProductSuggestion** (`SuggestionStatus`), **PriceDecision** — pricing outputs

Vector columns (768D) use the pgvector extension. Embedding writes use raw SQL (`execute_raw`) rather than the Prisma ORM.

### Data Flow

```
Merchant side:
  ShopifyProduct/Variant (sync) → shopify-semantic-worker → embedding-worker → vectors

Discovery + competitor side:
  dynamicPricingEnabled product → scheduler-worker (beat) → discovery-worker (Serper)
    → CompetitorCandidate → scraper-worker (Firecrawl) → extraction-worker (Groq)
    → ScrapedProduct/Variant + GCS → semantic-worker → embedding-worker → vectors

Pricing:
  matcher-worker (vector sim) → ProductMatch
    → elasticity-worker → VariantCompetitorStats
    → pricing-worker → PriceDecision
    → suggestion-worker → ProductSuggestion (surfaced in UI; v1 is suggestion-only)
```

## Environment Variables

Required in `.env`:

```
DATABASE_URL=postgresql://...
REDIS_URL=redis://localhost:6379/0
FIRECRAWL_API_KEY=...
GROQ_API_KEY=...
SERPER_API_KEY=...
GCS_IMAGE_BUCKET=...
GCS_MARKDOWN_BUCKET=...
VERTEX_PROJECT=...
VERTEX_LOCATION=...
GOOGLE_APPLICATION_CREDENTIALS=...
```

## Key Tech

- **Prisma**: Two clients — JS (`shopify_ui/`) and Python (`prisma-client-py` in `services/`). Schema lives at `shopify_ui/prisma/schema.prisma`; run `npm run setup` in `shopify_ui/` after schema changes.
- **Celery**: Queue routing and beat schedule defined in `services/common/celery_app.py`. All tasks use the `@app.task` decorator. Beat ticks live in `services/scraper_svc/celery_beat.py`.
- **pgvector**: Enabled via the Prisma schema's `extensions`. Vector writes use raw SQL.
- **uv**: Python package manager. Use `uv run` for all Python commands; don't activate a virtualenv manually.
- **Shopify CLI**: Required for frontend dev. `npm run dev` starts the tunnel automatically.
