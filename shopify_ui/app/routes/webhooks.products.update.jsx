// webhooks.products.update.jsx — PRODUCTS_UPDATE webhook. Pure forward of
// the raw payload to Python's handle_product_update task — no mapping/
// upsert logic here (that's the correct pattern; webhooks.products.create.jsx
// mirrors it rather than reimplementing the mapping in JS).
import { authenticate } from "../shopify.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";

export const action = async ({ request }) => {
  const { topic, shop, payload } = await authenticate.webhook(request);

  if (topic !== "PRODUCTS_UPDATE") {
    return new Response("Unhandled topic", { status: 422 });
  }

  try {
    await fetch(`${PYTHON_API_URL}/internal/shopify/product-update-webhook`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Token": process.env.INTERNAL_API_TOKEN },
      body: JSON.stringify({ shop_domain: shop, payload }),
    });
  } catch (err) {
    console.error("[webhook] Failed to forward product update to API gateway:", err);
  }

  return new Response(null, { status: 200 });
};
