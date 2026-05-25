/**
 * internal.apply-chat-price.jsx — chatbot-driven Shopify price write.
 *
 * Called by chatbot_svc /apply-price tool with {preview_id}.
 * Re-validates the ChatPreview row (existence, expiry, single-use, kind),
 * computes new prices using the FROZEN variantIds + change, then issues
 * Shopify Admin productVariantsBulkUpdate. Marks ChatPreview.appliedAt
 * and stores per-variant succeeded/failed result.
 *
 * Required env:
 *   INTERNAL_API_TOKEN — shared secret matched against X-Internal-Token
 *
 * This route is NOT for browser/Shopify-originated requests — it has no
 * Shopify HMAC validation and no embedded auth. The shared-secret header
 * is its only auth surface, so the env var must be a high-entropy random.
 */
import prisma from "../db.server";
import shopify from "../shopify.server";

const VARIANT_BULK_UPDATE = `
  mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
    productVariantsBulkUpdate(productId: $productId, variants: $variants) {
      productVariants { id price }
      userErrors { field message }
    }
  }
`;

function computeNewPrice(current, change) {
  const c = Number(current ?? 0);
  if (change.type === "percent") return Math.round(c * (1 + change.value / 100) * 100) / 100;
  if (change.type === "absolute") return Math.round((c + change.value) * 100) / 100;
  if (change.type === "set") return Math.round(change.value * 100) / 100;
  throw new Error(`bad change type: ${change.type}`);
}

export const action = async ({ request }) => {
  if (request.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }

  const expected = process.env.INTERNAL_API_TOKEN;
  if (!expected || request.headers.get("x-internal-token") !== expected) {
    return new Response("forbidden", { status: 403 });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("invalid json", { status: 400 });
  }

  const { preview_id, applied_by } = body || {};
  if (!preview_id) {
    return Response.json({ ok: false, reason: "missing preview_id" }, { status: 400 });
  }

  const preview = await prisma.chatPreview.findUnique({ where: { id: preview_id } });
  if (!preview) {
    return Response.json({ ok: false, reason: "preview_not_found" }, { status: 404 });
  }
  if (preview.appliedAt) {
    return Response.json({ ok: false, reason: "already_applied" }, { status: 409 });
  }
  if (preview.expiresAt < new Date()) {
    return Response.json({ ok: false, reason: "expired" }, { status: 410 });
  }
  if (preview.kind !== "price_change") {
    return Response.json({ ok: false, reason: "wrong_kind" }, { status: 400 });
  }

  const variants = await prisma.shopifyVariant.findMany({
    where: { id: { in: preview.variantIds } },
    select: { id: true, productId: true, currentPrice: true },
  });

  const change = preview.change;

  // unauthenticated.admin throws if the shop has no offline session row,
  // and the library handles Token Exchange refresh internally when the
  // stored token is past its expiry.
  let admin;
  try {
    ({ admin } = await shopify.unauthenticated.admin(preview.shopDomain));
  } catch (err) {
    return Response.json(
      { ok: false, reason: "no_session_for_shop", error: String(err?.message ?? err) },
      { status: 401 },
    );
  }

  // Group variants by product (productVariantsBulkUpdate is per-product)
  const byProduct = new Map();
  for (const v of variants) {
    const list = byProduct.get(v.productId) ?? [];
    list.push({ id: v.id, price: String(computeNewPrice(v.currentPrice, change)) });
    byProduct.set(v.productId, list);
  }

  const succeeded = [];
  const failed = [];
  for (const [productId, payload] of byProduct) {
    try {
      const resp = await admin.graphql(VARIANT_BULK_UPDATE, {
        variables: { productId, variants: payload },
      });
      const json = await resp.json();
      const errs = json?.data?.productVariantsBulkUpdate?.userErrors ?? [];
      if (errs.length) {
        const reason = errs.map((e) => e.message).join("; ");
        for (const v of payload) failed.push({ variant_id: v.id, reason });
      } else {
        for (const v of payload) succeeded.push(v.id);
      }
    } catch (err) {
      const reason = String(err?.message ?? err);
      for (const v of payload) failed.push({ variant_id: v.id, reason });
    }
  }

  await prisma.chatPreview.update({
    where: { id: preview_id },
    data: {
      appliedAt: new Date(),
      appliedBy: applied_by ?? null,
      result: { succeeded, failed },
    },
  });

  return Response.json({ ok: true, preview_id, succeeded, failed });
};
