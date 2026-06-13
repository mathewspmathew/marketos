import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";
import ChatPanel from "../components/chatbot/ChatPanel";

// Homepage = the assistant. The loader keeps the fresh-install sync bootstrap
// that used to live on the products list, so a never-synced store still kicks
// a background product pull when the merchant lands here.
export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  // Ensure ShopifyUser row exists for this shop
  await db.shopifyUser.upsert({
    where: { shopDomain },
    update: {},
    create: { shopDomain },
  });

  // Non-blocking auto-kick: enqueue a background pull if the DB was never
  // synced (fresh install / stale). Never await it — the loader is read-only.
  // ERROR is excluded so a persistent failure (e.g. expired offline token)
  // doesn't re-enqueue on every load — the Refresh/Retry button owns recovery.
  const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
  const user = await db.shopifyUser.findUnique({ where: { shopDomain } });
  const count = await db.shopifyProduct.count({ where: { shopDomain } });
  if (
    (count === 0 || user?.productSyncedAt == null) &&
    user?.productSyncState !== "SYNCING" &&
    user?.productSyncState !== "ERROR"
  ) {
    await db.shopifyUser.update({
      where: { shopDomain },
      data: { productSyncState: "SYNCING", productSyncStartedAt: new Date() },
    });
    void fetch(
      `${PYTHON_API_URL}/internal/shopify/sync-products?shop_domain=${encodeURIComponent(shopDomain)}`,
      { method: "POST" },
    ).catch(() => {});
  }

  return {};
};

export default function HomePage() {
  return (
    <s-page heading="Assistant">
      <ChatPanel />
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
