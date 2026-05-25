/**
 * app.apply-preview.jsx — Shopify-authed proxy that injects the internal
 * token server-side and forwards to internal.apply-chat-{price,flag}.
 *
 * The browser cannot hold INTERNAL_API_TOKEN; this hop bridges that.
 */
import { authenticate } from "../shopify.server";

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const { preview_id, kind } = await request.json();
  const path = kind === "dynamic_pricing_toggle"
    ? "/internal/apply-chat-flag"
    : "/internal/apply-chat-price";

  // Best practice: hit our own app URL via SHOPIFY_APP_URL or APP_URL env var.
  const base = process.env.APP_URL || process.env.SHOPIFY_APP_URL || "";
  const r = await fetch(`${base}${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-internal-token": process.env.INTERNAL_API_TOKEN ?? "",
    },
    body: JSON.stringify({
      preview_id,
      applied_by: session.userId ? String(session.userId) : null,
    }),
  });
  return new Response(await r.text(), {
    status: r.status,
    headers: { "content-type": "application/json" },
  });
};
