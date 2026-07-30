/**
 * internal.notify-confirm.jsx — sends the one-time confirmation email when
 * a merchant clicks "Notify" on Settings. app.settings.jsx's action calls
 * this directly (same app, same process) and only flips
 * priceChangeNotificationsEnabled to true if this returns ok:true.
 *
 * Required env: INTERNAL_API_TOKEN, RESEND_API_KEY, RESEND_FROM_EMAIL.
 */
import { deriveStoreName } from "../lib/storeName";
import { sendEmail } from "../lib/resendClient.server";
import { renderConfirmationEmail } from "../emails/notificationsEnabledConfirmation.server";

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

  const { shopDomain, email } = body || {};
  if (!shopDomain || !email) {
    return Response.json({ ok: false, reason: "missing fields" }, { status: 400 });
  }

  const storeName = deriveStoreName(shopDomain);
  const { subject, html } = await renderConfirmationEmail({ storeName, email });
  const result = await sendEmail({ to: email, subject, html });

  return Response.json(result);
};
