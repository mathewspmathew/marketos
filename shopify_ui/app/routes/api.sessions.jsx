/**
 * api.sessions.jsx — authenticated proxy to chatbot_svc session list / clear-all.
 *   GET    /api/sessions        -> { ok, sessions: [{id, title, updated_at, message_count}] }
 *   DELETE /api/sessions        -> { ok, deleted }
 * The shop is taken from the authenticated session, never from the client.
 *
 * Resource-route nesting note: api.sessions.$id.jsx is a flat-routes child of
 * this file. For data requests to /api/sessions/:id only the $id loader/action
 * runs — the parent loader/action here is NOT invoked. Do NOT add a default-export
 * component to this file; if you do it becomes a layout parent and the child route
 * would need an <Outlet> to render.
 */
import { authenticate } from "../shopify.server";

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const url = `${process.env.CHATBOT_SVC_URL}/sessions?shop_domain=${encodeURIComponent(session.shop)}`;
  const upstream = await fetch(url);
  if (!upstream.ok) return Response.json({ ok: false, sessions: [] }, { status: 502 });
  const data = await upstream.json();
  return Response.json({ ok: true, sessions: data.sessions ?? [] });
};

export const action = async ({ request }) => {
  if (request.method !== "DELETE") return new Response("method not allowed", { status: 405 });
  const { session } = await authenticate.admin(request);
  const url = `${process.env.CHATBOT_SVC_URL}/sessions?shop_domain=${encodeURIComponent(session.shop)}`;
  const upstream = await fetch(url, { method: "DELETE" });
  if (!upstream.ok) return Response.json({ ok: false }, { status: 502 });
  return Response.json(await upstream.json());
};
