user will toggle the dynamic pricing with the details. 

serper api will scrap the product links saved in CompetitorCandidate
from here ProductUrl table is updated for permanent watcher is needed.

what if product direct url is not getting - only we got some search page in flipkart - implement the older technology (not yet implemented)

after scraping products ->
firecrawl - uploads the page to bucket - grok takes the text information. - this can be done in scheduled way.

  1. Save & start dynamic pricing on Products page → ShopifyProduct.dynamicPricingEnabled=true.
  2. Within ≤30s, celery_beat logs [Beat] enqueued discovery for product ….
  3. discovery_worker picks the task off discovery_queue, writes N rows to CompetitorCandidate

rare case: if we get search link in the listing page of many products: (happens when we have bad search query)


2. scraper-worker
firecrawl api take the link and scrapes. upload to GCS bucket.
fires scraper.extract_candidate(candidate_id, gcs_ref) on extraction_queue.

3. Extract (extract_candidate)
 - extraction-worker picks it up, downloads markdown from GCS.
 - Groq parses it into a ScrapedProduct (title, vendor, variants, prices, image, etc.).
 - Uploads product image to GCS.

- DB write inside one txn:
    - Upsert ScrapedProduct by checking existing ProductUrl.url.
    - Upsert ProductUrl with shopifyProductId, prodId, nextRunAt (cadence from product/shop settings) → this is the row the
  rescrape tick will use later.

as of now no rediscovery is happening (with Serper api)

ScrapedVariant - with semanticText
ScrapedProduct - product
ProductUrl - for rescraping

ShopifyEmbedding - save embedding of all the shopify variants

ProductMatch - shopifyVariantId, competitorVariantId ---- Variant ↔ Variant ---- M × N (many merchant variants × many  competitor variants)  --- (one row per product pair)

ProductLevelMatch - shopifyProductId, scrapedProductId --- Product ↔ Product ---- 1 (one row per product pair) 




what happens - when rescrap is done:

15-STEP COMPLETE EXECUTION FLOW

PHASE 1: Beat Scheduler (30s tick)
1. Beat finds ProductUrl rows where nextRunAt <= NOW()
2. Atomic UPDATE: nextRunAt = NULL (guard against double-dispatch)
3. Dispatch 5 scraper tasks to Celery queue

PHASE 2: Scraper Worker
4. Worker picks up task, verifies ProductUrl is active
5. Fetch frequency config (5 minutes)
6. Firecrawl scrapes competitor URL → markdown
7. Groq extracts product data → JSON

PHASE 3: Database Updates
8. UPDATE ScrapedVariant - currentPrice, originalPrice, isInStock, stockQuantity, updatedAt
9. INSERT CompetitorPriceObservation (NEW ROW) ← FEEDS STATS PAGE
10. UPDATE ProductUrl - lastScrapedAt = NOW()

PHASE 4: Reschedule
11. Calculate next run time: NOW() + frequency (5 min)
12. UPDATE ProductUrl - nextRunAt = 2026-06-30T19:37:18 ← YOUR FIX HERE

PHASE 5: Background
13. Matcher worker recalculates confidence (async)
14. Pricing worker updates stats & creates decisions (async)

PHASE 6: Stats Page
15. User sees fresh price data with new observations ✓

---
8 Database Tables Modified (In Order)

┌─────┬────────────────────────────┬────────┬─────────────────────────────┐
│  #  │           Table            │ Action │             Why             │
├─────┼────────────────────────────┼────────┼─────────────────────────────┤
│ 1   │ ProductUrl                 │ UPDATE │ nextRunAt = NULL (claim)    │
├─────┼────────────────────────────┼────────┼─────────────────────────────┤
│ 2   │ ProductUrl                 │ UPDATE │ lastScrapedAt = NOW()       │
├─────┼────────────────────────────┼────────┼─────────────────────────────┤
│ 3   │ ScrapedVariant             │ UPDATE │ Price/stock from extraction │
├─────┼────────────────────────────┼────────┼─────────────────────────────┤
│ 4   │ CompetitorPriceObservation │ INSERT │ Price snapshot (KEY!)       │
├─────┼────────────────────────────┼────────┼─────────────────────────────┤
│ 5   │ ProductUrl                 │ UPDATE │ nextRunAt = NOW() + freq    │
├─────┼────────────────────────────┼────────┼─────────────────────────────┤
│ 6   │ VariantCompetitorStats     │ UPDATE │ Min/max/avg recalculated    │
├─────┼────────────────────────────┼────────┼─────────────────────────────┤
│ 7   │ ProductMatch               │ UPDATE │ Confidence recalculated     │
├─────┼────────────────────────────┼────────┼─────────────────────────────┤
│ 8   │ PriceDecision              │ INSERT │ Pricing recommendation      │
└─────┴────────────────────────────┴────────┴─────────────────────────────┘