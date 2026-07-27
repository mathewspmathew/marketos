/**
 * internal.apply-chat-price.jsx — chatbot-driven Shopify price write.
 *
 * Called by chatbot_svc /apply-price tool with {preview_id}.
 * Re-validates the ChatPreview row (existence, expiry, single-use, kind),
 * reads the new prices Python already computed and froze into
 * preview.summary.newPriceByVariant at preview time (not recomputed here —
 * see services/chatbot_svc/tools/preview.py's _compute_new_price, the single
 * source of truth), then issues Shopify Admin productVariantsBulkUpdate.
 * Marks ChatPreview.appliedAt and stores per-variant succeeded/failed result.
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

  // Both depend only on `preview` (already fetched above), not on each
  // other — run concurrently instead of two sequential round-trips.
  const [variants, adminResult] = await Promise.all([
    prisma.shopifyVariant.findMany({
      where: { id: { in: preview.variantIds } },
      select: { id: true, productId: true, currentPrice: true },
    }),
    // unauthenticated.admin throws if the shop has no offline session row,
    // and the library handles Token Exchange refresh internally when the
    // stored token is past its expiry.
    shopify.unauthenticated.admin(preview.shopDomain).then(
      (result) => ({ ok: true, result }),
      (err) => ({ ok: false, err }),
    ),
  ]);

  if (!adminResult.ok) {
    return Response.json(
      { ok: false, reason: "no_session_for_shop", error: String(adminResult.err?.message ?? adminResult.err) },
      { status: 401 },
    );
  }
  const { admin } = adminResult.result;

  // Group variants by product (productVariantsBulkUpdate is per-product).
  // Prices come from the preview's frozen newPriceByVariant map — not
  // recomputed — so apply always matches what the merchant was shown.
  const newPriceByVariant = preview.summary?.newPriceByVariant ?? {};
  const byProduct = new Map();
  const succeeded = [];
  const failed = [];
  for (const v of variants) {
    const newPrice = newPriceByVariant[v.id];
    if (newPrice == null) {
      failed.push({ variant_id: v.id, reason: "price_not_in_preview" });
      continue;
    }
    const list = byProduct.get(v.productId) ?? [];
    list.push({ id: v.id, price: String(newPrice) });
    byProduct.set(v.productId, list);
  }

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
