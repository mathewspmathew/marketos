/**
 * webhooks.orders.create
 *
 * Increments SalesAggregate counters for each line item whose variant we
 * already track. Counters are best-effort: a daily worker (shopify_sync_svc,
 * step 8) rebuilds the rolling 7d/28d window from Shopify Admin API using
 * the offline access token to correct any drift.
 *
 * For variants we don't yet have a SalesAggregate row for, we create one on
 * first order.
 */
import { authenticate } from "../shopify.server";
import db from "../db.server";

export const action = async ({ request }) => {
  const { topic, shop, payload } = await authenticate.webhook(request);

  if (topic !== "ORDERS_CREATE") {
    return new Response("Unhandled topic", { status: 422 });
  }

  const order = payload;
  const lineItems = Array.isArray(order.line_items) ? order.line_items : [];

  for (const li of lineItems) {
    if (!li.variant_id) continue;
    const variantId = `gid://shopify/ProductVariant/${li.variant_id}`;

    // Skip if we don't track this variant — webhook can arrive before the
    // products/create webhook on a brand-new product.
    const exists = await db.shopifyVariant.findUnique({
      where: { id: variantId },
      select: { id: true },
    });
    if (!exists) continue;

    const qty = Number(li.quantity ?? 0);
    const price = Number(li.price ?? 0);
    const revenue = qty * price;

    await db.salesAggregate.upsert({
      where: { shopifyVariantId: variantId },
      update: {
        orders7d:   { increment: qty },
        orders28d:  { increment: qty },
        revenue7d:  { increment: revenue },
        revenue28d: { increment: revenue },
      },
      create: {
        shopifyVariantId: variantId,
        shopDomain: shop,
        orders7d:   qty,
        orders28d:  qty,
        revenue7d:  revenue,
        revenue28d: revenue,
      },
    });
  }

  return new Response(null, { status: 200 });
};
