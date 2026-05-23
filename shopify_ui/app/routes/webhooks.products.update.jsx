import { authenticate } from "../shopify.server";
import db from "../db.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";

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

  // 3. Upsert ShopifyVariants with manual-price-edit detection.
  // If currentPrice changes and no PriceDecision wrote it in the last 60s,
  // treat as a merchant manual edit: re-anchor basePrice to the new value
  // so the lifetime cap respects the merchant's new intent.
  const MANUAL_EDIT_WINDOW_MS = 60 * 1000;
  const manualEditCutoff = new Date(Date.now() - MANUAL_EDIT_WINDOW_MS);
  let anyManualEdit = false;

  if (Array.isArray(product.variants)) {
    for (const v of product.variants) {
      const variantId = `gid://shopify/ProductVariant/${v.id}`;
      const options   = {};
      if (v.option1) options["Option1"] = v.option1;
      if (v.option2) options["Option2"] = v.option2;
      if (v.option3) options["Option3"] = v.option3;

      const prior = await db.shopifyVariant.findUnique({
        where: { id: variantId },
        select: { currentPrice: true },
      });
      const newPrice = parseFloat(v.price);
      const priorPrice = prior?.currentPrice != null ? Number(prior.currentPrice) : null;
      const priceChanged = priorPrice != null && Number.isFinite(newPrice)
        && Math.abs(newPrice - priorPrice) > 0.005;

      let isManualEdit = false;
      if (priceChanged) {
        // Did the pricing pipeline write this in the last 60s? If so it's ours.
        const recent = await db.priceDecision.findFirst({
          where: {
            shopifyVariantId: variantId,
            appliedAt: { gte: manualEditCutoff },
          },
          select: { id: true },
        });
        isManualEdit = !recent;
      }

      await db.shopifyVariant.upsert({
        where: { id: variantId },
        update: {
          title:             v.title,
          currentPrice:      v.price,
          compareAtPrice:    v.compare_at_price ?? null,
          sku:               v.sku    ?? null,
          barcode:           v.barcode ?? null,
          options,
          inventoryQuantity: v.inventory_quantity ?? null,
          semanticText:      null, // reset so pipeline regenerates embedding
          // Manual edit re-anchors this variant's lifetime cap to the new price.
          ...(isManualEdit ? { basePrice: v.price } : {}),
        },
        create: {
          id:                variantId,
          productId:         shopifyId,
          title:             v.title,
          currentPrice:      v.price,
          compareAtPrice:    v.compare_at_price ?? null,
          sku:               v.sku    ?? null,
          barcode:           v.barcode ?? null,
          options,
          inventoryQuantity: v.inventory_quantity ?? null,
        },
      });

      if (isManualEdit) {
        anyManualEdit = true;
        console.log(`[webhook] manual price edit detected on ${variantId}: ${priorPrice} → ${newPrice}. basePrice re-anchored.`);
      }
    }
  }

  // If any variant was re-anchored, recompute product basePrice = min variant base.
  // Also clear lastDecisionAt so the next rescrape cycle re-evaluates immediately
  // against the new anchor instead of waiting out the debounce.
  if (anyManualEdit) {
    const newMin = await db.shopifyVariant.aggregate({
      where: { productId: shopifyId },
      _min:  { basePrice: true },
    });
    if (newMin._min.basePrice != null) {
      await db.shopifyProduct.update({
        where: { id: shopifyId },
        data: { basePrice: newMin._min.basePrice, lastDecisionAt: null },
      });
    }
  }

  // Trigger semantic + embedding pipeline via the internal API gateway
  try {
    await fetch(`${PYTHON_API_URL}/internal/shopify/product-updated?product_id=${encodeURIComponent(shopifyId)}`, {
      method: "POST",
    });
  } catch (err) {
    console.error("[webhook] Failed to notify API gateway:", err);
  }

  return new Response(null, { status: 200 });
};
