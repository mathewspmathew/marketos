import { shopify } from "../shopify.server.js";

// Forward OAuth tokens to MarketOS backend
const MARKETOS_API_URL = process.env.MARKETOS_API_URL || "https://your-marketos-domain.com";

export async function authCallback(request) {
  const { searchParams } = new URL(request.url);
  const shop = searchParams.get("shop");
  const code = searchParams.get("code");
  const hmac = searchParams.get("hmac");
  const state = searchParams.get("state");

  console.log(`OAuth callback received for shop: ${shop}`);

  try {
    // Verify HMAC signature (Shopify handles this)
    if (!shop || !code || !hmac) {
      return new Response("Missing required parameters", { status: 400 });
    }

    // Exchange authorization code for access token
    const { admin } = await shopify.authenticate.admin(request);
    const session = await admin.rest.session.getCurrent();
    
    const accessToken = session.accessToken;
    const scopes = session.scope;

    console.log(`Access token obtained for shop: ${shop}`);

    // Forward token to MarketOS backend
    const response = await fetch(`${MARKETOS_API_URL}/auth/callback`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        shop: shop,
        code: code,
        hmac: hmac,
        state: state,
        access_token: accessToken,
        scopes: scopes
      })
    });

    if (!response.ok) {
      console.error(`Failed to forward token to MarketOS: ${response.status}`);
      return new Response("Token forwarding failed", { status: 500 });
    }

    const result = await response.json();
    console.log(`Successfully forwarded token to MarketOS for shop: ${shop}`);

    // Redirect to embedded app or success page
    return Response.redirect(`${MARKETOS_API_URL}/auth/success?shop=${shop}`);

  } catch (error) {
    console.error("Error in OAuth callback:", error);
    return new Response("OAuth callback failed", { status: 500 });
  }
}
