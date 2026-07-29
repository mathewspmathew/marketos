/**
 * app.stats.stream.jsx — SSE proxy for the live-updating stats pages
 * (both app.stats._index.jsx and app.stats.$productId.jsx share this one
 * stream; app.stats.$productId.jsx filters on the event's product_id
 * client-side). Same header-forwarding rationale as app.matches.stream.jsx.
 */
import { authenticate } from "../shopify.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_HEADERS = { "X-Internal-Token": process.env.INTERNAL_API_TOKEN };

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  const upstream = await fetch(
    `${PYTHON_API_URL}/internal/dynamic-pricing/stats/stream?shop_domain=${encodeURIComponent(shopDomain)}`,
    { headers: INTERNAL_HEADERS, signal: request.signal },
  );

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
};
