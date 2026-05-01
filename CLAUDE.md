# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MarketOS is a Shopify-embedded e-commerce intelligence platform for dynamic pricing and competitive analysis. It has two main parts:
- A React Router frontend (embedded Shopify app) in `shopify_ui/`
- Python Celery microservices for scraping, extraction, and vector embeddings in `services/`

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

# Run individual workers
uv run celery -A services.common.celery_app worker -Q scraping_queue,extraction_queue -n scraper-worker
uv run celery -A services.common.celery_app worker -Q embedding_queue -n embedding-worker
uv run celery -A services.common.celery_app worker -Q scheduler_queue -n scheduler-worker
uv run celery -A services.common.celery_app beat
```

### Full Stack (Docker)

```bash
docker-compose up    # Redis + all Python workers + beat scheduler
```

## Architecture

### Frontend (`shopify_ui/`)

React Router 7 app embedded in Shopify via `@shopify/shopify-app-react-router`. Routes live in `shopify_ui/app/routes/`. Uses Prisma JS client to store Shopify OAuth sessions and read product/config data from the shared PostgreSQL database.

### Python Celery Workers (`services/`)

Four active workers with distinct Celery queues:

| Worker | Queues | Responsibility |
|--------|--------|----------------|
| scraper-worker | `scraping_queue`, `extraction_queue` | Firecrawl scraping → Groq LLM extraction → save to DB + GCS |
| embedding-worker | `embedding_queue` | Vertex AI text & image embeddings → pgvector write |
| scheduler-worker | `scheduler_queue` | Poll for idle scraping configs, queue scraping tasks |
| celery-beat | — | Triggers `check_idle_configs` every 30s |

Shared utilities in `services/common/`: Celery app config (`celery_app.py`), Prisma Python client (`db.py`), GCS helpers (`gcs_utils.py`), Pydantic schemas (`schemas.py`).

The remaining service directories (`api_gateway/`, `chatbot_svc/`, `product_svc/`, `rag_svc/`, etc.) are stubs.

### Database (Prisma + pgvector)

Schema at `prisma/schema.prisma` — shared by both JS and Python Prisma clients. Key models:

- **Session** — Shopify OAuth sessions
- **User** / **ShopifyProduct** / **ShopifyVariant** — merchant's own store data
- **ScrapedProduct** / **ScrapedVariant** — competitor product data from scraping
- **ScrapingConfig** — per-user scraping job configuration

Vector columns (768D) on product/variant models use the pgvector extension. Embedding writes use raw SQL (`execute_raw`) rather than the Prisma ORM.

### Data Flow

```
ScrapingConfig (UI) → scheduler-worker → scraper-worker
  → Firecrawl scrape → Groq extract → ScrapedProduct (DB) + GCS
  → embedding-worker → Vertex AI → vector columns (DB)
```

## Environment Variables

Required in `.env`:

```
DATABASE_URL=postgresql://...
REDIS_URL=redis://localhost:6379/0
FIRECRAWL_API_KEY=...
GROQ_API_KEY=...
GCS_IMAGE_BUCKET=...
GCS_MARKDOWN_BUCKET=...
VERTEX_PROJECT=...
VERTEX_LOCATION=...
GOOGLE_APPLICATION_CREDENTIALS=...
```

## Key Tech

- **Prisma**: Two clients — JS (`shopify_ui/`) and Python (`prisma-client-py` in `services/`). Run `npm run setup` in `shopify_ui/` after schema changes.
- **Celery**: Queue routing defined in `services/common/celery_app.py`. All tasks use `app.task` decorator.
- **pgvector**: Enabled via `prisma/schema.prisma` extensions. Vector writes use raw SQL.
- **uv**: Python package manager. Use `uv run` for all Python commands; don't activate virtualenv manually.
- **Shopify CLI**: Required for frontend dev. `npm run dev` starts the tunnel automatically.
