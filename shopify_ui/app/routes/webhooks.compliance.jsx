import { authenticate } from "../shopify.server";
import db from "../db.server";

// Shopify's three mandatory compliance topics share a single subscription
// (compliance_topics in shopify.app.toml) and are all delivered to this one
// URI — differentiated by `topic`, not by separate routes.
export const action = async ({ request }) => {
  const { shop, topic, session } = await authenticate.webhook(request);

  try {
    switch (topic) {
      case "CUSTOMERS_DATA_REQUEST":
      case "CUSTOMERS_REDACT":
        // MarketOS stores no Shopify customer PII (no Customer model), so
        // there is no data to compile or erase for this shop's customer.
        console.log(`Received ${topic} webhook for ${shop} — no customer data stored`);
        break;

      case "SHOP_REDACT":
        // Shopify can deliver this up to 48h after app/uninstalled. If a
        // session exists, the shop re-installed in the meantime — skip the
        // erase so we don't destroy live, actively-installed data.
        if (session) {
          console.log(`Received ${topic} webhook for ${shop} — shop has reinstalled, skipping erase`);
          break;
        }

        console.log(`Received ${topic} webhook for ${shop} — erasing all shop data`);
        // Deletion order matters: most children only cascade from their
        // immediate parent (per schema.prisma), not from ShopifyUser directly,
        // so shopDomain-scoped rows must be removed bottom-up before ShopifyUser.
        await db.$transaction([
          db.chatSession.deleteMany({ where: { shopDomain: shop } }),
          db.competitorPriceObservation.deleteMany({ where: { shopDomain: shop } }),
          db.productMatch.deleteMany({ where: { shopDomain: shop } }),
          db.productLevelMatch.deleteMany({ where: { shopDomain: shop } }),
          db.productEmbedding.deleteMany({ where: { shopDomain: shop } }),
          db.productUrl.deleteMany({ where: { shopDomain: shop } }),
          db.priceDecision.deleteMany({ where: { shopDomain: shop } }),
          db.competitorCandidate.deleteMany({ where: { shopDomain: shop } }),
          db.discoveryJob.deleteMany({ where: { shopDomain: shop } }),
          db.scrapedProduct.deleteMany({ where: { shopDomain: shop } }),
          db.shopifyProduct.deleteMany({ where: { shopDomain: shop } }),
          db.scrapingConfig.deleteMany({ where: { shopDomain: shop } }),
          db.shopSettings.deleteMany({ where: { shopDomain: shop } }),
          db.session.deleteMany({ where: { shop } }),
          db.shopifyUser.deleteMany({ where: { shopDomain: shop } }),
        ]);
        break;

      default:
        return new Response("Unhandled topic", { status: 422 });
    }
  } catch (err) {
    // Mandatory compliance webhook — Shopify allows up to 30 days to
    // complete a request and retries on non-2xx, so a real failure must
    // surface as a 500 here, not a swallowed 200 like the non-critical
    // product webhooks (webhooks.products.create/update.jsx) use.
    console.error(`[webhook] ${topic} failed for ${shop}:`, err);
    return new Response("Internal error processing compliance webhook", { status: 500 });
  }

  return new Response();
};
