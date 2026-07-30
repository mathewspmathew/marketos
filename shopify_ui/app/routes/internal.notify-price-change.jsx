/**
 * internal.notify-price-change.jsx — Python-triggered price-change email
 *
 * services/pricing_svc/apply.py POSTs here, fire-and-forget, after a
 * successful auto-apply. This route re-reads ShopSettings.notifyEmail
 * itself (not trusted from the request body) so the DB row always stays
 * the single source of truth for who receives these emails.
 *
 * Required env: INTERNAL_API_TOKEN (shared secret), RESEND_API_KEY,
 * RESEND_FROM_EMAIL (checked inside sendEmail).
 */
import db from "../db.server";
import { deriveStoreName } from "../lib/storeName";
import { sendEmail } from "../lib/resendClient.server";
import { renderPriceChangeEmail } from "../emails/priceChangeNotification.server";

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

  const { shopDomain, productTitle, currency, variants } = body || {};
  if (!shopDomain || !productTitle || !currency || !Array.isArray(variants) || variants.length === 0) {
    return Response.json({ ok: false, reason: "missing fields" }, { status: 400 });
  }

  const settings = await db.shopSettings.findUnique({ where: { shopDomain } });
  if (!settings?.notifyEmail) {
    return Response.json({ ok: false, reason: "no_notify_email" });
  }

  const storeName = deriveStoreName(shopDomain);
  const { subject, html } = await renderPriceChangeEmail({ storeName, productTitle, currency, variants });
  const result = await sendEmail({ to: settings.notifyEmail, subject, html });

  return Response.json(result);
};
