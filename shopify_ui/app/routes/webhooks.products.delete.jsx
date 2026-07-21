// webhooks.products.delete.jsx — PRODUCTS_DELETE webhook. Removes the local
// ShopifyProduct row (no-op if it was never synced).
import { authenticate } from "../shopify.server";
import db from "../db.server";

export const action = async ({ request }) => {
  const { topic, payload } = await authenticate.webhook(request);

  if (topic !== "PRODUCTS_DELETE") {
    return new Response("Unhandled topic", { status: 422 });
  }

  const shopifyId = `gid://shopify/Product/${payload.id}`;

  // Ignore if the product was never synced to our DB
  try {
    await db.shopifyProduct.delete({ where: { id: shopifyId } });
  } catch (_e) {
    // Record not found — that's fine
  }

  return new Response(null, { status: 200 });
};
