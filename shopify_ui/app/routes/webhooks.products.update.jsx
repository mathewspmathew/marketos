import { authenticate } from "../shopify.server";
import db from "../db.server";

export const action = async ({ request }) => {
  const { topic, shop, payload } = await authenticate.webhook(request);

  if (topic !== "PRODUCTS_UPDATE") {
    return new Response("Unhandled topic", { status: 422 });
  }

  const product = payload;
  const price =
    product.variants?.[0]?.price ?? "0.00";
  const compareAtPrice =
    product.variants?.[0]?.compare_at_price ?? null;
  const imageUrl =
    product.image?.src ?? product.images?.[0]?.src ?? null;
  const tags = JSON.stringify(
    product.tags ? product.tags.split(", ").filter(Boolean) : [],
  );
  const shopifyId = `gid://shopify/Product/${product.id}`;

  // Only update the Shopify-sourced fields; preserve MarketOS settings
  await db.product.upsert({
    where: { id: shopifyId },
    update: {
      title: product.title ?? "",
      description: product.body_html ?? "",
      price,
      compareAtPrice,
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
    },
    create: {
      id: shopifyId,
      shop,
      title: product.title ?? "",
      description: product.body_html ?? "",
      price,
      compareAtPrice,
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
    },
  });

  return new Response(null, { status: 200 });
};
