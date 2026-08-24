/**
 * api.chat.jsx — authenticated proxy to chatbot_svc.
 *
 * Forwards shop + user to chatbot_svc /chat, consumes the SSE response
 * server-side, and returns a single JSON payload:
 *   { ok, session_id, events: [{type, data}, ...] }
 *
 * Why buffer instead of stream: Shopify CLI's trycloudflare tunnel buffers
 * SSE, so the browser never sees events live. One-shot JSON is reliable
 * and the chatbot only emits whole-turn messages today.
 */
import { authenticate } from "../shopify.server";

export const action = async ({ request }) => {
  if (request.method !== "POST") return new Response("method not allowed", { status: 405 });
  const { session } = await authenticate.admin(request);
  const body = await request.json();

  const upstream = await fetch(`${process.env.CHATBOT_SVC_URL}/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      shop_domain: session.shop,
      user_id: session.userId ? String(session.userId) : null,
      session_id: body.session_id ?? null,
      message: body.message,
    }),
  });

  if (!upstream.ok || !upstream.body) {
    return Response.json(
      { ok: false, reason: "upstream_error", status: upstream.status },
      { status: 502 },
    );
  }

  const reader = upstream.body.getReader();
  const decoder = new TextDecoder();
  let raw = "";
  // eslint-disable-next-line no-constant-condition -- intentional read-until-done loop
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    raw += decoder.decode(value, { stream: true });
  }

  const events = [];
  let session_id = null;
  for (const block of raw.split(/\r?\n\r?\n/)) {
    if (!block.trim()) continue;
    const lines = block.split(/\r?\n/);
    const evLine = lines.find((l) => l.startsWith("event:"));
    const dataLine = lines.find((l) => l.startsWith("data:"));
    const type = evLine ? evLine.slice(7).trim() : "text";
    let data = {};
    try { data = JSON.parse((dataLine ?? "data: {}").slice(5).trim() || "{}"); } catch { /* malformed SSE data line, keep default */ }
    if (type === "open" && data.session_id) session_id = data.session_id;
    else events.push({ type, data });
  }

  return Response.json({ ok: true, session_id, events });
};
