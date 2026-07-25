/**
 * app.matches.lazy.jsx — resource route: one product's expanded match list
 * (top N or "all"), fetched on demand by app.matches.jsx via fetcher.load
 * so the main matches page doesn't have to load every product's full match
 * list up front. Data comes from services/pricing_svc/product_stats.py
 * (list_matches_for_product) via /internal/dynamic-pricing/matches/product.
 */
import { authenticate } from "../shopify.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_HEADERS = { "X-Internal-Token": process.env.INTERNAL_API_TOKEN };

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;
  const url = new URL(request.url);
  const productId = url.searchParams.get("productId");
  const limit = url.searchParams.get("limit") ? parseInt(url.searchParams.get("limit")) : 3;

  if (!productId) {
    throw new Response("productId required", { status: 400 });
  }

  const res = await fetch(
    `${PYTHON_API_URL}/internal/dynamic-pricing/matches/product` +
      `?shop_domain=${encodeURIComponent(shopDomain)}&product_id=${encodeURIComponent(productId)}&limit=${limit}`,
    { headers: INTERNAL_HEADERS },
  );
  const data = await res.json();
  if (!data.ok) throw new Response(data.error ?? "Failed to load matches.", { status: 502 });

  return { matches: data.matches, totalCount: data.totalCount };
};
