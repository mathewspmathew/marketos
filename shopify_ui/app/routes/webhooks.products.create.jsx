import { authenticate } from "../shopify.server";
import db from "../db.server";

export const action = async ({ request }) => {
  const { topic, shop, payload } = await authenticate.webhook(request);

  if (topic !== "PRODUCTS_CREATE") {
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

  await db.product.upsert({
    where: { id: `gid://shopify/Product/${product.id}` },
    update: {
      title: product.title ?? "",
      description: product.body_html ?? "",
      price,
      compareAtPrice,
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
      vectorized: false, // Reset on update (unlikely for create but good for consistency)
    },
    create: {
      id: `gid://shopify/Product/${product.id}`,
      shop,
      title: product.title ?? "",
      description: product.body_html ?? "",
      price,
      compareAtPrice,
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
      source: "INTERNAL",
      vectorized: false,
    },
  });

  return new Response(null, { status: 200 });
};
