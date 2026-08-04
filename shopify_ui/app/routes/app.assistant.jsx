import { useLoaderData } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";
import ChatPanel from "../components/chatbot/ChatPanel";

// The assistant/chatbot page, at /app/assistant. Products (at /app) now owns
// the fresh-install sync-bootstrap kick since it's the default landing page.
export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  // Ensure ShopifyUser row exists for this shop — independent of shopSettings
  // (different models, both keyed only on shopDomain), so run concurrently.
  const [, shopSettings] = await Promise.all([
    db.shopifyUser.upsert({
      where: { shopDomain },
      update: {},
      create: { shopDomain },
    }),
    db.shopSettings.findUnique({ where: { shopDomain } }),
  ]);

  return { currency: shopSettings?.currency };
};

export default function AssistantPage() {
  const { currency } = useLoaderData();
  return (
    <s-page heading="Assistant">
      <ChatPanel currency={currency} />
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
