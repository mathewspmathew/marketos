/**
 * internal.apply-chat-flag.jsx — chatbot-driven dynamicPricingEnabled toggle.
 *
 * Called by chatbot_svc with {preview_id, applied_by, action, + enable-only
 * fields}. The request is action-based ("enable" | "pause" | "resume" |
 * "delete"), not target-based: the caller says what button was clicked, and
 * this route decides whether that action is legal right now.
 *
 * After the token/preview guards it does a click-time state re-check — the
 * card is a frozen snapshot, so the product may have been toggled from the
 * product pane (or another tab) since the card rendered. It derives the
 * product's current card state (FRESH | ACTIVE | PAUSED) from
 * dynamicPricingEnabled + the competitor-candidate count and 409s with
 * "state_changed" if it no longer matches preview.change.cardState, then
 * enforces a per-state action whitelist before mutating. Field edits in the
 * body are honored only on "enable"; resume/pause/delete treat the card as
 * read-only. No Shopify call.
 *
 * Required env:
 *   INTERNAL_API_TOKEN — shared secret matched against X-Internal-Token
 *
 * This route is NOT for browser/Shopify-originated requests — it has no
 * Shopify HMAC validation and no embedded auth. The shared-secret header
 * is its only auth surface, so the env var must be a high-entropy random.
 */
import prisma from "../db.server";
import { deleteCompetitorData } from "../lib/competitorTeardown.server";
import { computeNextRunAt } from "../lib/frequency.server";

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

  const ALLOWED = {
    FRESH: ["enable"],
    ACTIVE: ["pause", "delete"],
    PAUSED: ["resume", "delete"],
  };
  const action = body.action;
  if (!action || !["enable", "pause", "resume", "delete"].includes(action)) {
    return Response.json({ ok: false, reason: "missing_action" }, { status: 400 });
  }

  const productId = preview.variantIds[0]; // panel previews freeze exactly one product id
  const shopDomain = preview.shopDomain;

  const product = await prisma.shopifyProduct.findFirst({
    where: { id: productId, shopDomain },
  });
  if (!product) {
    return Response.json({ ok: false, reason: "product_not_found" }, { status: 404 });
  }

  // Click-time state re-check: the card is a frozen snapshot; the product may
  // have been toggled from the product pane (or another tab) since it rendered.
  const candidateCount = await prisma.competitorCandidate.count({
    where: { shopDomain, shopifyProductId: productId },
  });
  const currentState = product.dynamicPricingEnabled
    ? "ACTIVE"
    : candidateCount > 0 ? "PAUSED" : "FRESH";
  const cardState = preview.change?.cardState;
  if (currentState !== cardState) {
    return Response.json(
      { ok: false, reason: "state_changed", cardState, currentState },
      { status: 409 },
    );
  }
  if (!ALLOWED[currentState].includes(action)) {
    return Response.json({ ok: false, reason: "action_not_allowed" }, { status: 400 });
  }

  const clamp = (v, lo, hi, dflt) => {
    const n = parseInt(v, 10);
    if (Number.isNaN(n)) return dflt;
    return Math.max(lo, Math.min(n, hi));
  };

  if (action === "enable") {
    const numResults = clamp(body.numResults, 1, 50, 10);
    const listingExpansionCap = clamp(body.listingExpansionCap, 1, 50, 5);
    await prisma.shopifyProduct.updateMany({
      where: { id: productId, shopDomain },
      data: { dynamicPricingEnabled: true, discoveryNumResults: numResults, listingExpansionCap },
    });
    if (typeof body.query === "string" && body.query.trim()) {
      await prisma.shopifyProduct.updateMany({
        where: { id: productId, shopDomain },
        data: { searchQueryOverride: body.query.trim() },
      });
    }
    await prisma.productUrl.updateMany({
      where: {
        shopifyProductId: productId,
        status: "ACTIVE",
        OR: [{ nextRunAt: null }, { nextRunAt: { lte: new Date() } }],
      },
      data: { nextRunAt: new Date() },
    });
    if (body.rescrape) {
      const query =
        body.query?.trim() || product.searchQueryOverride || product.searchQuery || product.title || "";
      if (query) {
        await prisma.discoveryJob.create({
          data: { shopDomain, shopifyProductId: productId, status: "QUEUED",
                  query, numResults, listingExpansionCap },
        });
      }
    }
  } else if (action === "resume") {
    // Read-only card: field edits in the body are deliberately ignored.
    await prisma.shopifyProduct.updateMany({
      where: { id: productId, shopDomain },
      data: { dynamicPricingEnabled: true },
    });
    const nextRunAt = computeNextRunAt(product.frequencyInterval, product.frequencyUnit);
    await prisma.productUrl.updateMany({
      where: {
        shopifyProductId: productId,
        status: "ACTIVE",
        OR: [{ nextRunAt: null }, { nextRunAt: { lte: new Date() } }],
      },
      data: { nextRunAt: nextRunAt ?? new Date() },
    });
  } else {
    // pause and delete both turn the flag off and stop in-flight work.
    await prisma.shopifyProduct.updateMany({
      where: { id: productId, shopDomain },
      data: { dynamicPricingEnabled: false },
    });
    await prisma.discoveryJob.updateMany({
      where: { shopDomain, shopifyProductId: productId, status: { in: ["QUEUED", "RUNNING"] } },
      data: { status: "FAILED", error: "cancelled: dynamic pricing turned off" },
    });
    await prisma.competitorCandidate.deleteMany({
      where: { shopDomain, shopifyProductId: productId, status: "PENDING" },
    });
    if (action === "delete") {
      await deleteCompetitorData(prisma, shopDomain, productId);
    }
  }

  await prisma.chatPreview.update({
    where: { id: preview_id },
    data: {
      appliedAt: new Date(),
      appliedBy: applied_by ?? null,
      result: { action, productId },
    },
  });

  return Response.json({ ok: true, preview_id, action, productId });
};
