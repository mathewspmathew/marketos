/**
 * internal.apply-chat-flag.jsx — chatbot-driven dynamicPricingEnabled toggle.
 *
 * Called by chatbot_svc with {preview_id}. Re-validates the ChatPreview row
 * (kind must be "dynamic_pricing_toggle"), updates the flag on the frozen
 * product set, and records the result. No Shopify call.
 *
 * Required env:
 *   INTERNAL_API_TOKEN — shared secret matched against X-Internal-Token
 *
 * This route is NOT for browser/Shopify-originated requests — it has no
 * Shopify HMAC validation and no embedded auth. The shared-secret header
 * is its only auth surface, so the env var must be a high-entropy random.
 */
import prisma from "../db.server";

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
  if (preview.kind !== "dynamic_pricing_toggle") {
    return Response.json({ ok: false, reason: "wrong_kind" }, { status: 400 });
  }

  const enabled = !!preview.change?.enabled;
  const productIds = preview.variantIds; // overloaded — holds product ids for flag previews

  const upd = await prisma.shopifyProduct.updateMany({
    where: { id: { in: productIds }, shopDomain: preview.shopDomain },
    data: { dynamicPricingEnabled: enabled },
  });

  const succeeded = productIds.slice(0, upd.count);
  const failed = [];

  await prisma.chatPreview.update({
    where: { id: preview_id },
    data: {
      appliedAt: new Date(),
      appliedBy: applied_by ?? null,
      result: { succeeded, failed, updatedCount: upd.count },
    },
  });

  return Response.json({ ok: true, preview_id, succeeded, failed, updatedCount: upd.count });
};
