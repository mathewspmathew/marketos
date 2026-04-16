import { shopify } from "../shopify.server.js";

// Forward Shopify webhooks to MarketOS backend
const MARKETOS_WEBHOOK_URL = process.env.MARKETOS_WEBHOOK_URL || "https://your-marketos-domain.com/webhooks/shopify";

export async function productsWebhook(request) {
  const { topic, shop, body, webhookId } = request;
  
  console.log(`Products webhook received: ${topic} for shop: ${shop}`);
  
  try {
    // Forward webhook to MarketOS backend
    const response = await fetch(MARKETOS_WEBHOOK_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Shopify-Topic': topic,
        'X-Shopify-Shop-Domain': shop,
        'X-Shopify-Webhook-Id': webhookId
      },
      body: JSON.stringify(body)
    });
    
    if (!response.ok) {
      console.error(`Failed to forward webhook to MarketOS: ${response.status}`);
      return new Response("Webhook forwarding failed", { status: 500 });
    }
    
    console.log(`Successfully forwarded ${topic} webhook to MarketOS`);
    return new Response("OK", { status: 200 });
    
  } catch (error) {
    console.error("Error forwarding webhook to MarketOS:", error);
    return new Response("Internal server error", { status: 500 });
  }
}
