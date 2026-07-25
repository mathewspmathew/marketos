import { useLoaderData } from "react-router";
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

  // Non-blocking auto-kick: ask Python to claim a sync slot if the shop was
  // never synced (fresh install / stale). Never await it — the loader is
  // read-only. Python owns the atomic claim + the ERROR exclusion (a
  // persistent failure shouldn't re-enqueue on every load — the
  // Refresh/Retry button on the Products page owns recovery from ERROR).
  const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
  void fetch(
    `${PYTHON_API_URL}/internal/shopify/sync-products` +
    `?shop_domain=${encodeURIComponent(shopDomain)}&only_if_never_synced=true&skip_if_error=true`,
    { method: "POST", headers: { "X-Internal-Token": process.env.INTERNAL_API_TOKEN } },
  ).catch(() => {});

  const shopSettings = await db.shopSettings.findUnique({ where: { shopDomain } });

  return { currency: shopSettings?.currency };
};

export default function HomePage() {
  const { currency } = useLoaderData();
  return (
    <s-page heading="Assistant">
      <ChatPanel currency={currency} />
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
