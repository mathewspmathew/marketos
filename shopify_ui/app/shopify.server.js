/**
 * shopify.server.js — configures the Shopify app SDK (API keys, scopes,
 * session storage, auth) and re-exports its helpers app-wide. afterAuth
 * seeds ShopifyUser/ShopSettings rows on every install/re-auth so a shop
 * always has real settings from the moment it installs.
 */
import "@shopify/shopify-app-react-router/adapters/node";
import {
  ApiVersion,
  AppDistribution,
  shopifyApp,
} from "@shopify/shopify-app-react-router/server";
import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";
import prisma from "./db.server";
import { DEFAULTS as SHOP_SETTINGS_DEFAULTS } from "./lib/shopSettingsDefaults.server";
import { shopAndHostParamsAreSafe } from "./lib/validateShopHostParams";

const shopify = shopifyApp({
  apiKey: process.env.SHOPIFY_API_KEY,
  apiSecretKey: process.env.SHOPIFY_API_SECRET || "",
  apiVersion: ApiVersion.October25,
  scopes: process.env.SCOPES?.split(","),
  appUrl: process.env.SHOPIFY_APP_URL || "",
  authPathPrefix: "/auth",
  sessionStorage: new PrismaSessionStorage(prisma),
  distribution: AppDistribution.AppStore,
  hooks: {
    // Seed a ShopSettings row (with defaults) the moment a shop finishes
    // install/re-auth, so pages like app.products.jsx never see a null
    // shopDefaults — previously ShopSettings only existed after someone
    // opened the Settings page, which could leave a fresh install's first
    // product save rejected with no visible error (see app.products.jsx
    // audit, 2026-07-20).
    afterAuth: async ({ session }) => {
      const shopDomain = session.shop;
      try {
        await prisma.shopifyUser.upsert({
          where: { shopDomain },
          update: {},
          create: { shopDomain },
        });
        await prisma.shopSettings.upsert({
          where: { shopDomain },
          update: {},
          create: { shopDomain, ...SHOP_SETTINGS_DEFAULTS, updatedAt: new Date() },
        });
      } catch (err) {
        // Re-throw rather than swallow: silently continuing here would
        // reintroduce the exact bug this hook exists to prevent (a shop
        // installed with no ShopSettings row, rejecting its first save
        // with no visible error — see comment above).
        console.error(`[afterAuth] failed to seed ShopifyUser/ShopSettings for ${shopDomain}:`, err);
        throw err;
      }
    },
  },
  future: {
    // Expiring tokens are required for App Store distribution. Background
    // workers no longer talk to Shopify directly — they POST to the
    // /internal/apply-price route on this app, which uses
    // shopify.unauthenticated.admin() to get an Admin client that handles
    // Token Exchange refresh transparently.
    expiringOfflineAccessTokens: true,
  },
  ...(process.env.SHOP_CUSTOM_DOMAIN
    ? { customShopDomains: [process.env.SHOP_CUSTOM_DOMAIN] }
    : {}),
});

export default shopify;
export const apiVersion = ApiVersion.October25;
export const addDocumentResponseHeaders = shopify.addDocumentResponseHeaders;

// Hardened `authenticate.admin` (see validateShopHostParams.js for the full
// story): the SDK's sanitizeHost() crashes with `TypeError: Invalid URL` when
// a request carries a valid `shop` param plus a base64-valid `host` param that
// decodes to a non-URL (e.g. "hello world") — reproduces Sentry issue
// PYTHON-7A as a 500. If the params are malformed in that specific way,
// short-circuit with the same app-bridge Response the SDK throws for a
// missing shop param; garbage traffic gets a bounce instead of a 500.
function authenticateWithGuard(method) {
  return async (request) => {
    if (!shopAndHostParamsAreSafe(request)) {
      // Mirrors renderAppBridge(): same script App Bridge needs to re-embed
      // the app and retry with correct shop/host params.
      throw new Response(
        `\n      <script data-api-key="${process.env.SHOPIFY_API_KEY}" src="https://cdn.shopify.com/shopifycloud/app-bridge.js"></script>\n    `,
        {
          headers: {
            "content-type": "text/html;charset=utf-8",
          },
        },
      );
    }
    return shopify.authenticate[method](request);
  };
}

export const authenticate = {
  ...shopify.authenticate,
  admin: authenticateWithGuard("admin"),
};
export const unauthenticated = shopify.unauthenticated;
export const login = shopify.login;
export const registerWebhooks = shopify.registerWebhooks;
export const sessionStorage = shopify.sessionStorage;
