/**
 * app.matches.stream.jsx — SSE proxy for the live-updating matches page.
 * Browser EventSource cannot set the X-Internal-Token header api_gateway's
 * internal routes require, so this resource route authenticates the
 * merchant via the normal Shopify session cookie (same as every other
 * app.*.jsx loader) and pipes api_gateway's SSE byte stream straight
 * through untouched. No JSON parsing/re-encoding — the response body is
 * forwarded as-is so event framing survives intact.
 */
import { authenticate } from "../shopify.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_HEADERS = { "X-Internal-Token": process.env.INTERNAL_API_TOKEN };

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  try {
    const upstream = await fetch(
      `${PYTHON_API_URL}/internal/dynamic-pricing/matches/stream?shop_domain=${encodeURIComponent(shopDomain)}`,
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
  } catch (err) {
    // The client tearing down its connection (tab closed, page navigated
    // away, eventStream.js reconnecting) aborts this same request.signal we
    // forwarded above — that's the intended cleanup path (see live_updates.py
    // unsubscribe), not an application error, so it must not propagate as an
    // unhandled exception.
    if (err.name === "AbortError") return new Response(null, { status: 204 });
    throw err;
  }
};
