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
import { deleteCompetitorData } from "../lib/competitorTeardown.server";

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
  const shopDomain = preview.shopDomain;
  const clamp = (v, lo, hi, dflt) => {
    const n = parseInt(v, 10);
    if (Number.isNaN(n)) return dflt;
    return Math.max(lo, Math.min(n, hi));
  };

  // "pause" unless the merchant explicitly chose delete (disable branch only).
  const mode = body.mode === "delete" ? "delete" : "pause";

  // Persist the merchant's chosen scrape breadth onto the product on enable, so
  // the first competitor fetch uses these numbers whether it runs now (the
  // "Rescrape now" DiscoveryJob below) or shortly (the beat's first-time
  // discovery, which reads discoveryNumResults; the scrape resolves the cap
  // via Product.listingExpansionCap — see scraper_svc/candidate.py).
  const numResults = clamp(body.numResults, 1, 50, 10);
  const listingExpansionCap = clamp(body.listingExpansionCap, 1, 50, 5);

  const upd = await prisma.shopifyProduct.updateMany({
    where: { id: { in: productIds }, shopDomain },
    data: enabled
      ? { dynamicPricingEnabled: true, discoveryNumResults: numResults, listingExpansionCap }
      : { dynamicPricingEnabled: false },
  });

  if (enabled) {
    // Re-arm known ProductUrls so the rescrape loop resumes (mirrors the
    // discover page's toggle-on behavior).
    await prisma.productUrl.updateMany({
      where: {
        shopifyProductId: { in: productIds },
        status: "ACTIVE",
        OR: [{ nextRunAt: null }, { nextRunAt: { lte: new Date() } }],
      },
      data: { nextRunAt: new Date() },
    });
    // Persist the merchant's chosen/edited search query so discovery — this run
    // and future rescrapes — uses it. The DiscoveryJob query computation below
    // reads searchQueryOverride first.
    if (typeof body.query === "string" && body.query.trim()) {
      await prisma.shopifyProduct.updateMany({
        where: { id: { in: productIds }, shopDomain },
        data: { searchQueryOverride: body.query.trim() },
      });
    }
    // "Rescrape now" gates the immediate, credit-spending discovery. When off,
    // the beat's first-time discovery picks the product up shortly using the
    // numbers persisted onto it above.
    if (body.rescrape) {
      for (const pid of productIds) {
        const product = await prisma.shopifyProduct.findFirst({
          where: { id: pid, shopDomain },
        });
        const query =
          product?.searchQueryOverride || product?.searchQuery || product?.title || "";
        if (query) {
          await prisma.discoveryJob.create({
            data: { shopDomain, shopifyProductId: pid, status: "QUEUED",
                    query, numResults, listingExpansionCap },
          });
        }
      }
    }
  } else {
    for (const pid of productIds) {
      // Cooperative-cancel the in-flight run: stop the job, drop only its
      // not-yet-scraped (PENDING) candidates. The scrape_candidate guard
      // halts any task already dispatched (flag is now false).
      await prisma.discoveryJob.updateMany({
        where: { shopDomain, shopifyProductId: pid, status: { in: ["QUEUED", "RUNNING"] } },
        data: { status: "FAILED", error: "cancelled: dynamic pricing turned off" },
      });
      await prisma.competitorCandidate.deleteMany({
        where: { shopDomain, shopifyProductId: pid, status: "PENDING" },
      });
      if (mode === "delete") {
        await deleteCompetitorData(prisma, shopDomain, pid);
      }
    }
  }

  const succeeded = productIds.slice(0, upd.count);
  const failed = [];

  await prisma.chatPreview.update({
    where: { id: preview_id },
    data: {
      appliedAt: new Date(),
      appliedBy: applied_by ?? null,
      result: { succeeded, failed, updatedCount: upd.count, enabled,
                mode: enabled ? undefined : mode },
    },
  });

  return Response.json({ ok: true, preview_id, succeeded, failed, updatedCount: upd.count });
};
