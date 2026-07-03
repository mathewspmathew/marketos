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
      title:           product.title        ?? "",
      description:     product.body_html    ?? "",
      tags,
      productType:     product.product_type ?? "",
      imageUrl,
      status:          product.status?.toUpperCase() ?? "ACTIVE",
      semanticStatus:  "PENDING",
      semanticVersion: { increment: 1 },
    },
    create: {
      id:              shopifyId,
      shopDomain:      shop,
      title:           product.title        ?? "",
      description:     product.body_html    ?? "",
      tags,
      productType:     product.product_type ?? "",
      imageUrl,
      status:          product.status?.toUpperCase() ?? "ACTIVE",
      semanticStatus:  "PENDING",
    },
  });

  // Snapshot the bounds inputs BEFORE any re-anchor, so we can later tell
  // whether the stored min/max were auto-derived from the old anchor.
  const priorProduct = await db.shopifyProduct.findUnique({
    where: { id: shopifyId },
    select: { avgBasePrice: true, minPriceOverride: true, maxPriceOverride: true },
  });

  // 3. Upsert ShopifyVariants with manual-price-edit detection.
  // A price change is the pricing engine's own write-back iff it equals the
  // latest applied PriceDecision.newPrice — webhook delivery can lag, so
  // price identity (not a time window) is the signal. Anything else is a
  // merchant manual edit: re-anchor basePrice so the lifetime cap follows
  // the merchant's new intent.
  let anyManualEdit = false;
  const manualEdits = []; // { variantId, oldPrice, newPrice }

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
        const latest = await db.priceDecision.findFirst({
          where: { shopifyVariantId: variantId, appliedAt: { not: null } },
          orderBy: { decidedAt: "desc" },
          select: { newPrice: true },
        });
        const engineWrote = latest != null
          && Math.abs(Number(latest.newPrice) - newPrice) <= 0.005;
        isManualEdit = !engineWrote;
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
          basePrice:         v.price ?? null,
          updatedAt:         new Date(),
        },
      });

      if (isManualEdit) {
        anyManualEdit = true;
        manualEdits.push({ variantId, oldPrice: priorPrice, newPrice });
        console.log(`[webhook] manual price edit detected on ${variantId}: ${priorPrice} → ${newPrice}. basePrice re-anchored.`);
      }
    }
  }

  // avgBasePrice is derived (average of variant anchors) — recompute every
  // time so it tracks variant adds/removals and re-anchors. On a manual edit
  // also clear lastDecisionAt so the next rescrape cycle re-evaluates
  // immediately against the new anchor instead of waiting out the debounce.
  const avgBase = await db.shopifyVariant.aggregate({
    where: { productId: shopifyId },
    _avg:  { basePrice: true },
  });
  await db.shopifyProduct.update({
    where: { id: shopifyId },
    data: {
      avgBasePrice: avgBase._avg.basePrice,
      ...(anyManualEdit ? { lastDecisionAt: null, semanticStatus: "PENDING" } : {}),
    },
  });

  if (anyManualEdit) {
    // Stored min/max that EQUAL the formula output from the old anchor were
    // auto-derived — recompute them from the new anchor so the cap follows
    // the merchant's manual re-price. Merchant-typed bounds won't match the
    // formula and are left untouched.
    const settings = await db.shopSettings.findUnique({
      where: { shopDomain: shop },
      select: { lifetimeCapPct: true },
    });
    const cap = settings?.lifetimeCapPct != null ? Number(settings.lifetimeCapPct) : 0.25;
    const oldAvg    = priorProduct?.avgBasePrice     != null ? Number(priorProduct.avgBasePrice)     : null;
    const newAvg    = avgBase._avg.basePrice          != null ? Number(avgBase._avg.basePrice)        : null;
    const storedMin = priorProduct?.minPriceOverride != null ? Number(priorProduct.minPriceOverride) : null;
    const storedMax = priorProduct?.maxPriceOverride != null ? Number(priorProduct.maxPriceOverride) : null;
    const close = (a, b) => a != null && b != null && Math.abs(a - b) <= 0.011;
    if (oldAvg != null && newAvg != null && storedMin != null && storedMax != null
        && close(storedMin, oldAvg * (1 - cap)) && close(storedMax, oldAvg * (1 + cap))) {
      await db.shopifyProduct.update({
        where: { id: shopifyId },
        data: {
          minPriceOverride: (newAvg * (1 - cap)).toFixed(2),
          maxPriceOverride: (newAvg * (1 + cap)).toFixed(2),
        },
      });
      console.log(`[webhook] auto-derived bounds recomputed from new anchor ${newAvg} for ${shopifyId}`);
    }

    // Merchant edits on tracked variants become part of the price history so
    // the stats page (and chatbot) can show "merchant changed the price".
    for (const edit of manualEdits) {
      const tracked =
        (await db.productMatch.count({ where: { shopifyVariantId: edit.variantId } })) > 0 ||
        (await db.priceDecision.count({ where: { shopifyVariantId: edit.variantId } })) > 0;
      if (tracked && edit.oldPrice != null && Number.isFinite(edit.newPrice)) {
        await db.priceDecision.create({
          data: {
            shopDomain:       shop,
            shopifyVariantId: edit.variantId,
            oldPrice:         edit.oldPrice,
            newPrice:         edit.newPrice,
            reason:           "manual price edit by merchant",
            appliedAt:        new Date(),
            autoApplied:      false,
          },
        });
      }
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
