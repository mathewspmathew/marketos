user will toggle the dynamic pricing with the details. 

serper api will scrap the product links saved in CompetitorCandidate.url
from here ProductUrl table is updated for permanent watcher is needed.

what if product direct url is not getting - only we got some search page in flipkart - implement the older technology (not yet implemented)

after scraping products ->
firecrawl - uploads the page to bucket - grok takes the text information. - this can be done in scheduled way.

  1. Save & start dynamic pricing on Products page → ShopifyProduct.dynamicPricingEnabled=true.
  2. Within ≤30s, celery_beat logs [Beat] enqueued discovery for product ….
  3. discovery_worker picks the task off discovery_queue, writes N rows to CompetitorCandidate