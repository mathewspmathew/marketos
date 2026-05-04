import { authenticate } from "../shopify.server";
import db from "../db.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";

export const action = async ({ request }) => {
  const { topic, shop, payload } = await authenticate.webhook(request);

  if (topic !== "PRODUCTS_CREATE") {
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
      title:       product.title       ?? "",
      description: product.body_html   ?? "",
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
    },
    create: {
      id:          shopifyId,
      shopDomain:  shop,
      title:       product.title       ?? "",
      description: product.body_html   ?? "",
      tags,
      productType: product.product_type ?? "",
      imageUrl,
      status: product.status?.toUpperCase() ?? "ACTIVE",
    },
  });

  // 3. Upsert ShopifyVariants — reset semanticText so embeddings are regenerated
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
          sku:            v.sku   ?? null,
          barcode:        v.barcode ?? null,
          options,
        },
        create: {
          id:             variantId,
          productId:      shopifyId,
          title:          v.title,
          currentPrice:   v.price,
          compareAtPrice: v.compare_at_price ?? null,
          sku:            v.sku   ?? null,
          barcode:        v.barcode ?? null,
          options,
        },
      });
    }
  }

  // 4. Trigger semantic + embedding pipeline via the internal API gateway
  try {
    await fetch(`${PYTHON_API_URL}/internal/shopify/product-updated?product_id=${encodeURIComponent(shopifyId)}`, {
      method: "POST",
    });
  } catch (err) {
    // Non-fatal: the semantic worker will catch up on the next scheduled run
    console.error("[webhook] Failed to notify API gateway:", err);
  }

  return new Response(null, { status: 200 });
};
