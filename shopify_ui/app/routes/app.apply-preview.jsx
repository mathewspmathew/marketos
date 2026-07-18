/**
 * app.apply-preview.jsx — Shopify-authed proxy that injects the internal
 * token server-side and forwards to internal.apply-chat-price.
 *
 * The browser cannot hold INTERNAL_API_TOKEN; this hop bridges that.
 */
import { authenticate } from "../shopify.server";

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const body = await request.json();
  const { preview_id } = body;
  const path = "/internal/apply-chat-price";

  // Forward only the fields the internal route understands; never trust these
  // for auth (shop comes from the ChatPreview row server-side).
  const forward = {
    preview_id,
    applied_by: session.userId ? String(session.userId) : null,
  };

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
