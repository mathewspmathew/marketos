/**
 * api.query-studio.jsx — authenticated proxy to chatbot_svc /query-studio.
 *
 * Forwards the shop from the session and the product/focus/mode body to the
 * chatbot service, and returns its JSON: { ok, candidates: [{query, confidence, reason}] }.
 */
import { authenticate } from "../shopify.server";

export const action = async ({ request }) => {
  if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
  const { session } = await authenticate.admin(request);
  const body = await request.json();

  const upstream = await fetch(`${process.env.CHATBOT_SVC_URL}/query-studio`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      shop_domain: session.shop,
      product_id: body.product_id,
      focus: body.focus ?? "",
      mode: body.mode ?? "propose",
      prior: body.prior ?? null,
      instruction: body.instruction ?? null,
    }),
  });

  if (!upstream.ok) {
    return Response.json(
      { ok: false, reason: "upstream_error", status: upstream.status },
      { status: 502 },
    );
  }

  const data = await upstream.json().catch(() => ({ candidates: [] }));
  return Response.json({ ok: true, candidates: data.candidates ?? [] });
};
