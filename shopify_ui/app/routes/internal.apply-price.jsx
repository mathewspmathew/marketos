/**
 * internal.apply-price.jsx — internal Shopify write proxy
 *
 * Background Celery workers POST {shopDomain, productId, variants} here
 * instead of calling Shopify Admin directly. This route uses the official
 * @shopify/shopify-app-react-router library's `unauthenticated.admin()`,
 * which (a) reads the offline token from the Session table, (b) calls
 * Shopify's Token Exchange endpoint to refresh it on the fly when the
 * library detects expiry, and (c) writes the refreshed token back.
 *
 * Required env:
 *   INTERNAL_API_TOKEN — shared secret matched against X-Internal-Token
 *
 * This route is NOT for browser/Shopify-originated requests — it has no
 * Shopify HMAC validation and no embedded auth. The shared-secret header
 * is its only auth surface, so the env var must be a high-entropy random.
 */
import shopify from "../shopify.server";

const VARIANT_BULK_UPDATE_MUTATION = `
  mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
    productVariantsBulkUpdate(productId: $productId, variants: $variants) {
      productVariants { id price }
      userErrors { field message }
    }
  }
`;

export const action = async ({ request }) => {
  if (request.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }
  const expected = process.env.INTERNAL_API_TOKEN;
  if (!expected) {
    return new Response("server misconfigured: INTERNAL_API_TOKEN unset", { status: 500 });
  }
  if (request.headers.get("x-internal-token") !== expected) {
    return new Response("forbidden", { status: 403 });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("invalid json", { status: 400 });
  }

  const { shopDomain, productId, variants } = body || {};
  if (!shopDomain || !productId || !Array.isArray(variants) || variants.length === 0) {
    return Response.json(
      { ok: false, reason: "missing fields" },
      { status: 400 },
    );
  }

  // unauthenticated.admin throws if the shop has no offline session row,
  // and the library handles Token Exchange refresh internally when the
  // stored token is past its expiry.
  let admin;
  try {
    ({ admin } = await shopify.unauthenticated.admin(shopDomain));
  } catch (err) {
    return Response.json(
      { ok: false, reason: "no_session_for_shop", error: String(err?.message ?? err) },
      { status: 401 },
    );
  }

  let result;
  try {
    const resp = await admin.graphql(VARIANT_BULK_UPDATE_MUTATION, {
      variables: { productId, variants },
    });
    result = await resp.json();
  } catch (err) {
    return Response.json(
      { ok: false, reason: "graphql_failed", error: String(err?.message ?? err) },
      { status: 502 },
    );
  }

  return Response.json({ ok: true, data: result.data });
};
