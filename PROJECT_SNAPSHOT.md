# MarketOS — Project Snapshot

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
| Database | PostgreSQL + pgvector extension (768D vectors) |
| ORM | Prisma (shared schema, JS + Python clients) |
| Queue broker | Redis |
| Embeddings | Vertex AI (text-embedding-004 / multimodal) |
| Scraping | Firecrawl API |
| LLM extraction | Groq |
| Object storage | Google Cloud Storage |

---

## Core Idea (What's Built So Far)

A Shopify merchant installs the app, and it automatically monitors competitor product pages. For each competitor URL the merchant configures, the system:

1. Periodically scrapes the competitor page (Firecrawl)
2. Extracts structured product data — title, variants, prices, images — using an LLM (Groq)
3. Stores the raw markdown and images in GCS, and the structured data in PostgreSQL
4. Generates 768D text + image vector embeddings (Vertex AI) for each product/variant
5. Saves those vectors into pgvector for future similarity search and price comparison

The Shopify UI lets the merchant configure which competitor URLs to watch, how often, and how many products to track.

---

## User Flow Diagram

```
MERCHANT (Shopify Admin)
        |
        | installs app / OAuth
        v
+-------------------------+
|   Shopify UI            |
|   (React Router 7)      |
|   shopify_ui/           |
+-------------------------+
        |
        | merchant fills out ScrapingConfig
        | (competitor URL, frequency, product limit)
        v
+-------------------------+
|   PostgreSQL (Aiven)    |
|   ScrapingConfig table  |
+-------------------------+
        ^                 |
        |                 | celery-beat fires every 30s
        |                 v
        |    +---------------------------+
        |    |  scheduler-worker         |
        |    |  (scheduler_queue)        |
        |    |  checks IDLE configs      |
        |    |  sets status → QUEUED     |
        +----+---------------------------+
                          |
                          | dispatches scrape task
                          v
+--------------------------------------------------+
|  scraper-worker  (scraping_queue)                |
|                                                  |
|  1. Firecrawl API  →  raw markdown + images      |
|  2. Groq LLM       →  structured JSON            |
|     (title, variants, prices, specs)             |
|  3. Save markdown  →  GCS bucket                 |
|  4. Save images    →  GCS bucket                 |
|  5. Upsert ScrapedProduct / ScrapedVariant       |
|     + ProductUrl   →  PostgreSQL                 |
+--------------------------------------------------+
                          |
                          | dispatches embed task
                          v
+--------------------------------------------------+
|  embedding-worker  (embedding_queue)             |
|                                                  |
|  1. Fetch product text + image URL from DB       |
|  2. Vertex AI  →  768D text embedding            |
|  3. Vertex AI  →  768D image embedding           |
|  4. Raw SQL INSERT → ProductEmbedding table      |
|     (pgvector columns)                           |
+--------------------------------------------------+
                          |
                          v
+-------------------------+
|   PostgreSQL (Aiven)    |
|   ProductEmbedding      |
|   vectorText  vector(768)|
|   vectorImg   vector(768)|
+-------------------------+
                          |
                          | (next: similarity search
                          |  for price comparison)
                          v
                    [ TODO / future ]
                  RAG / pricing engine
```

### Data stored at each stage

```
ScrapingConfig    →  what to scrape (URL, schedule, limits)
ProductUrl        →  per-URL tracking (status, last scraped, next run)
ScrapedProduct    →  competitor product metadata
ScrapedVariant    →  per-variant price + stock
GCS markdown      →  raw page content backup
GCS images        →  competitor product images
ProductEmbedding  →  768D vectors for similarity / RAG
```
