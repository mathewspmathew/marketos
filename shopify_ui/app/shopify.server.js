import "@shopify/shopify-app-react-router/adapters/node";
import {
  ApiVersion,
  AppDistribution,
  shopifyApp,
} from "@shopify/shopify-app-react-router/server";
import { PrismaSessionStorage } from "@shopify/shopify-app-session-storage-prisma";
import prisma from "./db.server";
import { DEFAULTS as SHOP_SETTINGS_DEFAULTS } from "./lib/shopSettingsDefaults.server";

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
export const authenticate = shopify.authenticate;
export const unauthenticated = shopify.unauthenticated;
export const login = shopify.login;
export const registerWebhooks = shopify.registerWebhooks;
export const sessionStorage = shopify.sessionStorage;
