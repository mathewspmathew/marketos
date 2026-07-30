/**
 * resendClient.server.js — thin wrapper around the Resend SDK for
 * price-change and enable-confirmation notification emails. Never throws:
 * callers (internal.notify-price-change.jsx, internal.notify-confirm.jsx)
 * always get a plain {ok, error?} result back so a delivery failure can be
 * handled inline instead of crashing the request.
 */
import { Resend } from "resend";

export async function sendEmail({ to, subject, html }) {
  const apiKey = process.env.RESEND_API_KEY;
  const from = process.env.RESEND_FROM_EMAIL;
  if (!apiKey || !from) {
    return { ok: false, error: "server misconfigured: RESEND_API_KEY/RESEND_FROM_EMAIL unset" };
  }

  const resend = new Resend(apiKey);
  try {
    const { error } = await resend.emails.send({ from, to, subject, html });
    if (error) {
      return { ok: false, error: String(error.message ?? error) };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err?.message ?? err) };
  }
}
