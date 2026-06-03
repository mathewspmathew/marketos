/**
 * api.sessions.$id.jsx — authenticated proxy for one chat.
 *   GET    /api/sessions/:id    -> { ok, turns: [...] }   (rehydrate a chat)
 *   DELETE /api/sessions/:id    -> { ok }                 (delete one chat)
 * The shop is taken from the authenticated session, never from the client.
 */
import { authenticate } from "../shopify.server";

export const loader = async ({ request, params }) => {
  const { session } = await authenticate.admin(request);
  const url = `${process.env.CHATBOT_SVC_URL}/sessions/${encodeURIComponent(params.id)}/messages?shop_domain=${encodeURIComponent(session.shop)}`;
  const upstream = await fetch(url);
  if (upstream.status === 404) return Response.json({ ok: false, turns: [] }, { status: 404 });
  if (!upstream.ok) return Response.json({ ok: false, turns: [] }, { status: 502 });
  const data = await upstream.json();
  return Response.json({ ok: true, turns: data.turns ?? [] });
};

export const action = async ({ request, params }) => {
  if (request.method !== "DELETE") return new Response("method not allowed", { status: 405 });
  const { session } = await authenticate.admin(request);
  const url = `${process.env.CHATBOT_SVC_URL}/sessions/${encodeURIComponent(params.id)}?shop_domain=${encodeURIComponent(session.shop)}`;
  const upstream = await fetch(url, { method: "DELETE" });
  if (upstream.status === 404) return Response.json({ ok: false }, { status: 404 });
  if (!upstream.ok) return Response.json({ ok: false }, { status: 502 });
  return Response.json({ ok: true });
};
