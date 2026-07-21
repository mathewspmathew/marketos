// PRODUCTS_CREATE webhook — forwards the raw payload to Python's
// handle_product_update Celery task (same endpoint webhooks.products.update.jsx
// uses). That task does INSERT ... ON CONFLICT DO UPDATE, so it's a correct
// upsert for brand-new products too, and it enqueues the semantic pipeline
// itself — no separate mapping logic or product-updated call needed here.
import { authenticate } from "../shopify.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";

export const action = async ({ request }) => {
  const { topic, shop, payload } = await authenticate.webhook(request);

  if (topic !== "PRODUCTS_CREATE") {
    return new Response("Unhandled topic", { status: 422 });
  }

  try {
    await fetch(`${PYTHON_API_URL}/internal/shopify/product-update-webhook`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ shop_domain: shop, payload }),
    });
  } catch (err) {
    console.error("[webhook] Failed to forward product create to API gateway:", err);
  }

  return new Response(null, { status: 200 });
};
