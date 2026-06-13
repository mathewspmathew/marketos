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