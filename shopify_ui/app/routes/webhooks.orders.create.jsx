/**
 * webhooks.orders.create
 *
 * Currently a no-op. Sales-velocity tracking (SalesAggregate) was dropped in
 * the discovery_pivot migration; v1 pricing is competitor-driven only, with
 * no elasticity / velocity inputs. The webhook is left registered so Shopify
 * doesn't disable it; it just acks and exits.
 *
 * Re-introduce this when ML / sales-aware pricing comes back in v2.
 */
import { authenticate } from "../shopify.server";

export const action = async ({ request }) => {
  await authenticate.webhook(request);
  return new Response(null, { status: 200 });
};
