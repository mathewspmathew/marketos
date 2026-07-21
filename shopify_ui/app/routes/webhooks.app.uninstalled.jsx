// webhooks.app.uninstalled.jsx — APP_UNINSTALLED webhook. Deletes Session
// rows only; full shop-data erasure is deferred to webhooks.compliance.jsx's
// SHOP_REDACT handler per Shopify's compliance timing (not a bug).
import { authenticate } from "../shopify.server";
import db from "../db.server";

export const action = async ({ request }) => {
  const { shop, session, topic } = await authenticate.webhook(request);

  console.log(`Received ${topic} webhook for ${shop}`);

  // Webhook requests can trigger multiple times and after an app has already been uninstalled.
  // If this webhook already ran, the session may have been deleted previously.
  if (session) {
    await db.session.deleteMany({ where: { shop } });
  }

  return new Response();
};
