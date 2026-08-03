MarketOS — Technical Glance
===========================

One-line:
  Shopify-embedded dynamic-pricing + competitor-intelligence platform.

Top-level shape:
  shopify_ui/   → React Router 7 embedded Shopify app (frontend + webhooks)
  services/     → Python Celery microservices (backend pipeline)
  shopify_ui/prisma/schema.prisma → shared Postgres schema (pgvector)
  docker-compose.yml → Redis + workers + beat + api_gateway + chatbot_svc

Languages / runtimes:
  - TypeScript / React Router 7 (frontend)
  - Python 3.12 (services)
  - Node.js 20+ (frontend tooling, Shopify CLI)

Frontend stack (shopify_ui/), with resolved versions:
  - react                              18.3.1
  - react-router                       7.18.2
  - @shopify/shopify-app-react-router  1.1.0
  - @shopify/app-bridge-react          4.2.4
  - Shopify CLI                        dev tunnel (shopify binary, not an npm dependency)
  - @prisma/client / prisma            6.16.3
  - vite                               6.3.6
  - typescript                         5.9.3
  - ESLint

Backend stack (services/), with resolved versions:
  - celery[redis]            5.6.3   (queue routing in services/common/celery_app.py)
  - redis                    6.4.0   (broker + result backend + shop locks)
  - fastapi                  0.136.1 (api_gateway, chatbot_svc)
  - sqlalchemy               2.0.49  (+ prisma-client-py for shared schema access)
  - pydantic                 2.13.3
  - pydantic-ai              1.102.0 (chatbot agent)
  - litellm                  >=1.93.0 (Groq access via litellm.Router)
  - logfire                  >=4.33.0 (observability, chatbot_svc)
  - structlog                >=24.1
  - flower                   >=2.0.1 (Celery monitoring)
  - lightgbm, scikit-learn, numpy, joblib (matcher_svc scoring/ML)
  - uv (package manager, single root pyproject.toml + uv.lock)

Storage / infra:
  - PostgreSQL + pgvector (768D vector columns, HNSW indexes)
  - Redis (Celery broker + result backend + distributed shop locks)
  - Google Cloud Storage (raw markdown + product images)

External APIs, with resolved client versions:
  - Firecrawl (firecrawl-py 4.23.0)          — web scraping
  - Groq (groq 1.2.0 SDK)                    — LLM extraction, semantics, chat inference
  - Serper                                   — competitor discovery (Google SERP)
  - Vertex AI (google-cloud-aiplatform 1.148.1) — text-embedding-004 + multimodalembedding@001, 768D
  - Google Cloud Storage (google-cloud-storage 3.10.1)
  - Shopify Admin GraphQL — read (orders/inventory) + write (price update) via Token Exchange

Services (services/ subdirectory → queue → responsibility):
  api_gateway     HTTP                  internal entry for UI → Celery fan-out (/internal/* endpoints)
  scraper_svc     scraping_queue        Firecrawl fetch + GCS persist
                  extraction_queue      Groq → ScrapedProduct/Variant
                  semantic_queue        competitor variant semantic-text generation
                  shopify_semantic_queue merchant variant semantic-text generation
                  scheduler_queue       beat tick: rescrape due URLs + semantic backfill
  discovery_svc   discovery_queue       Serper → CompetitorCandidate
  embedding_svc   embedding_queue       Vertex AI → pgvector (merchant + competitor sides)
  matcher_svc     match_queue           vector similarity → ProductMatch / ProductLevelMatch
  pricing_svc     stats_queue           competitor price stats + elasticity inputs
                  pricing_queue         decide price + auto-apply when eligible; revert (sync, not a task)
  shopify_svc     shopify_sync_queue    merchant product sync + sales-aggregate rebuild; Shopify write-back
  chatbot_svc     HTTP/SSE              Pydantic-AI agent — chat, tool calls, apply/revert preview
  common          —                     shared: Celery app, db, models, GCS, embeddings, Groq, Shopify auth

  Runtime note: on the dev laptop one pricing-worker consumes
  stats_queue + pricing_queue + shopify_sync_queue together
  (split into separate workers in production for independent scaling).

Data model (shopify_ui/prisma/schema.prisma) — 20 models:
  Session, ShopSettings
  ShopifyUser, ShopifyProduct, ShopifyVariant, ShopifyEmbedding
  ScrapedProduct, ScrapedVariant, ProductEmbedding
  ScrapingConfig, ProductUrl (UrlStatus, ScrapeStatus)
  DiscoveryJob, CompetitorCandidate (DiscoveryStatus, CandidateStatus)
  ProductMatch, ProductLevelMatch (MatchConfidenceTier, MatchReviewStatus)
  CompetitorPriceObservation, VariantCompetitorStats
  PriceDecision (PricingTier)
  ChatSession, ChatMessage, ChatPreview (ChatRole, PreviewKind)

Pipeline (one line each):
  merchant install     → Shopify OAuth → Session
  product sync         → ShopifyProduct/Variant → shopify-semantic → embedding
  discovery            → Serper → CompetitorCandidate → merchant accept → ProductUrl
  competitor scrape    → Firecrawl → Groq extract → ScrapedProduct/Variant → semantic → embedding
  matching             → matcher_svc → ProductMatch / ProductLevelMatch (CONFIRMED, or LIKELY + merchant-confirmed)
  pricing              → stats → decide (LIKELY-gate) → PriceDecision → auto-apply when eligible
  revert               → api_gateway /internal endpoint → pricing_svc.revert → PriceDecision reverted
  chat                 → FastAPI SSE → Pydantic-AI agent → tools → DB / apply-preview → apply or revert

Run:
  Frontend: cd shopify_ui && npm run dev
  Backend : docker-compose up         (redis + all workers + beat + api_gateway + chatbot_svc)
  Schema  : cd shopify_ui && npm run setup   (prisma generate + migrate)
