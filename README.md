# MarketOS

A Shopify-embedded e-commerce intelligence platform for **dynamic pricing** and **competitive analysis**.

A merchant installs the app, points it at competitor URLs, and MarketOS continuously scrapes, extracts, embeds, matches, and suggests new titles / descriptions / prices for the merchant's catalogue — all reviewable from inside the Shopify Admin.

For a full architectural snapshot see [`PROJECT_SNAPSHOT.md`](PROJECT_SNAPSHOT.md). Visual data-flow diagram: [`docs/marketos_flow.png`](docs/marketos_flow.png).

---

## End-to-End User Flow (Sequence Diagram)

```mermaid
sequenceDiagram
    autonumber
    actor M as Merchant
    participant UI as Shopify App<br/>(UI + API Gateway)
    participant BE as Backend<br/>(Celery workers + Redis)
    participant DB as PostgreSQL<br/>+ GCS + pgvector
    participant EXT as External APIs<br/>(Firecrawl · Groq · Vertex AI · Shopify)

    M->>UI: 1 : Install app & configure competitor URLs
    UI->>DB: 2 : Save Session + ScrapingConfig
    UI-->>M: 3 : Confirm setup

    Note over BE: celery-beat ticks every 30s
    BE->>DB: 4 : Pick IDLE configs (nextScrapAt ≤ now)
    BE->>EXT: 5 : Firecrawl scrape competitor pages
    EXT-->>BE: 6 : Markdown + image URLs
    BE->>DB: 7 : Upload to GCS · upsert ScrapedProduct

    BE->>EXT: 8 : Groq LLM extract structured data
    EXT-->>BE: 9 : Title · variants · prices · specs
    BE->>DB: 10 : Upsert ScrapedVariant rows

    BE->>EXT: 11 : Groq semantic summary per variant
    EXT-->>BE: 12 : semanticText blobs
    BE->>DB: 13 : Write semanticText

    BE->>EXT: 14 : Vertex AI text + image embeddings (768D)
    EXT-->>BE: 15 : Vectors
    BE->>DB: 16 : Insert into pgvector (HNSW index)

    BE->>DB: 17 : Per-domain similarity search
    BE->>DB: 18 : Upsert ProductMatch (matchScore)

    M->>UI: 19 : Open Suggestions / click "Re-suggest"
    UI->>BE: 20 : POST /internal/suggestion/regenerate
    BE->>DB: 21 : Pull matched competitors (score ≥ 65)
    BE->>EXT: 22 : Groq aggregate copy + price stats
    EXT-->>BE: 23 : Title · description · price band
    BE->>DB: 24 : Upsert ProductSuggestion + VariantPriceSuggestion

    UI->>DB: 25 : Read suggestions
    UI-->>M: 26 : Show comparison + Apply button
    M->>UI: 27 : Approve / edit / Apply
    UI->>EXT: 28 : Shopify Admin API write-back
    EXT-->>UI: 29 : Ack
    UI->>DB: 30 : Mark suggestion applied
```

---

## Quick Start

### Frontend (`shopify_ui/`)

```bash
cd shopify_ui
npm run setup        # Prisma client + migrations
npm run dev          # Shopify CLI tunnel + Vite
```

### Python services

```bash
uv sync --frozen
docker-compose up    # Redis + all workers + beat + api-gateway
```

Required `.env`: `DATABASE_URL`, `REDIS_URL`, `FIRECRAWL_API_KEY`, `GROQ_API_KEY`, `GCS_IMAGE_BUCKET`, `GCS_MARKDOWN_BUCKET`, `VERTEX_PROJECT`, `VERTEX_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`.

---

## Project Layout

| Path | Purpose |
|------|---------|
| `shopify_ui/` | React Router 7 embedded Shopify app (Prisma JS) |
| `services/api_gateway/` | FastAPI internal API (Shopify webhooks + suggestion triggers) |
| `services/scraper_svc/` | Firecrawl scrape · Groq extract · semantic summary · beat scheduler |
| `services/embedding_svc/` | Vertex AI text + image embeddings → pgvector |
| `services/matcher_svc/` | Per-domain HNSW similarity → `ProductMatch` |
| `services/suggestion_svc/` | Pricing stats + Groq copy → `ProductSuggestion` |
| `services/common/` | Shared Celery app, Prisma Python client, GCS helpers, schemas |
| `shopify_ui/prisma/` | Shared Prisma schema + migrations |
| `scripts/generate_flow_diagram.py` | Regenerates `docs/marketos_flow.png` |

---

## Tech Stack

**Frontend**: React 18 · React Router 7 · Vite · Shopify App Bridge · Prisma JS
**Backend**: Python 3.12 · Celery 5 · FastAPI · Prisma (Python) · SQLAlchemy
**Data**: PostgreSQL + pgvector (HNSW, 768D) · Redis · Google Cloud Storage
**AI**: Firecrawl (scrape) · Groq llama-3.1-8b-instant (extract / semantics / copy) · Vertex AI (text + multimodal embeddings)
