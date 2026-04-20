import { authenticate } from "../shopify.server";
import db from "../db.server";

export const action = async ({ request }) => {
  const { topic, shop, payload } = await authenticate.webhook(request);

  if (topic !== "PRODUCTS_UPDATE") {
    return new Response("Unhandled topic", { status: 422 });
  }

  const product = payload;
  const imageUrl = product.image?.src ?? product.images?.[0]?.src ?? null;
  const tags = product.tags ? product.tags.split(", ").map(t => t.trim()).filter(Boolean) : [];
  const shopifyId = `gid://shopify/Product/${product.id}`;

  const demoUserId = process.env.MARKETOS_DEMO_TENANT_ID || "00000000-0000-0000-0000-000000000001";
  await db.user.upsert({
    where: { id: demoUserId },
    update: {},
    create: {
      id: demoUserId,
      email: "demo@marketos.io",
      username: "Demo User",
    },
  });

  // 1. Upsert Product
  await db.shopifyProduct.upsert({
    where: { id: shopifyId },
    update: {
      title: product.title ?? "",
      description: product.body_html ?? "",
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
      vectorized: false,
    },
    create: {
      id: shopifyId,
      userId: demoUserId,
      shop,
      title: product.title ?? "",
      description: product.body_html ?? "",
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
      vectorized: false,
    },
  });

  // 2. Sync Variants
  if (product.variants && Array.isArray(product.variants)) {
    for (const v of product.variants) {
      const variantId = `gid://shopify/ProductVariant/${v.id}`;
      const options = {};
      // In webhook payload, options are flat (option1, option2, option3)
      if (v.option1) options["Option1"] = v.option1;
      if (v.option2) options["Option2"] = v.option2;
      if (v.option3) options["Option3"] = v.option3;

      await db.shopifyVariant.upsert({
        where: { id: variantId },
        update: {
          title: v.title,
          currentPrice: v.price,
          originalPrice: v.compare_at_price,
          sku: v.sku,
          barcode: v.barcode,
          options,
        },
        create: {
          id: variantId,
          productId: shopifyId,
          userId: demoUserId,
          title: v.title,
          currentPrice: v.price,
          originalPrice: v.compare_at_price,
          sku: v.sku,
          barcode: v.barcode,
          options,
        },
      });
    }
  }

  return new Response(null, { status: 200 });
};
