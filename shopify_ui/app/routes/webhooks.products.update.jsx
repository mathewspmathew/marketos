import { authenticate } from "../shopify.server";
import db from "../db.server";

export const action = async ({ request }) => {
  const { topic, shop, payload } = await authenticate.webhook(request);

  if (topic !== "PRODUCTS_UPDATE") {
    return new Response("Unhandled topic", { status: 422 });
  }

  const product  = payload;
  const imageUrl = product.image?.src ?? product.images?.[0]?.src ?? null;
  const tags     = product.tags ? product.tags.split(", ").map(t => t.trim()).filter(Boolean) : [];
  const shopifyId = `gid://shopify/Product/${product.id}`;

  // 1. Ensure ShopifyUser row exists (keyed by shop domain)
  await db.shopifyUser.upsert({
    where:  { shopDomain: shop },
    update: {},
    create: { shopDomain: shop },
  });

  // 2. Upsert ShopifyProduct
  await db.shopifyProduct.upsert({
    where: { id: shopifyId },
    update: {
      title:       product.title        ?? "",
      description: product.body_html    ?? "",
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
    },
    create: {
      id:          shopifyId,
      shopDomain:  shop,
      title:       product.title        ?? "",
      description: product.body_html    ?? "",
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
    },
  });

  // 3. Upsert ShopifyVariants
  if (Array.isArray(product.variants)) {
    for (const v of product.variants) {
      const variantId = `gid://shopify/ProductVariant/${v.id}`;
      const options   = {};
      if (v.option1) options["Option1"] = v.option1;
      if (v.option2) options["Option2"] = v.option2;
      if (v.option3) options["Option3"] = v.option3;

      await db.shopifyVariant.upsert({
        where: { id: variantId },
        update: {
          title:          v.title,
          currentPrice:   v.price,
          compareAtPrice: v.compare_at_price ?? null,
          sku:            v.sku    ?? null,
          barcode:        v.barcode ?? null,
          options,
        },
        create: {
          id:             variantId,
          productId:      shopifyId,
          title:          v.title,
          currentPrice:   v.price,
          compareAtPrice: v.compare_at_price ?? null,
          sku:            v.sku    ?? null,
          barcode:        v.barcode ?? null,
          options,
        },
      });
    }
  }

  return new Response(null, { status: 200 });
};
