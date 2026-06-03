/**
 * app.apply-preview.jsx — Shopify-authed proxy that injects the internal
 * token server-side and forwards to internal.apply-chat-{price,flag}.
 *
 * The browser cannot hold INTERNAL_API_TOKEN; this hop bridges that.
 */
import { authenticate } from "../shopify.server";

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const body = await request.json();
  const { preview_id, kind } = body;
  const path = kind === "dynamic_pricing_toggle"
    ? "/internal/apply-chat-flag"
    : "/internal/apply-chat-price";

  // Forward only the fields the internal route understands; never trust these
  // for auth (shop comes from the ChatPreview row server-side).
  const forward = {
    preview_id,
    applied_by: session.userId ? String(session.userId) : null,
  };
  if (kind === "dynamic_pricing_toggle") {
    if (body.enable) {
      forward.rescrape = !!body.rescrape;
      forward.numResults = body.numResults;
      forward.listingExpansionCap = body.listingExpansionCap;
    } else {
      forward.mode = body.mode === "delete" ? "delete" : "pause";
    }
  }

  const base = process.env.APP_URL || process.env.SHOPIFY_APP_URL || "";
  const r = await fetch(`${base}${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-internal-token": process.env.INTERNAL_API_TOKEN ?? "",
    },
    body: JSON.stringify(forward),
  });
  return new Response(await r.text(), {
    status: r.status,
    headers: { "content-type": "application/json" },
  });
};
