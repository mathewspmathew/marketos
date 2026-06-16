import { json } from "react-router";
import { authenticate } from "../shopify.server";
import db from "../db.server";

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;
  const url = new URL(request.url);
  const productId = url.searchParams.get("productId");
  const limit = url.searchParams.get("limit") ? parseInt(url.searchParams.get("limit")) : 3;

  if (!productId) {
    throw new Response("productId required", { status: 400 });
  }

  // Count total matches for this product
  const totalCount = await db.productLevelMatch.count({
    where: {
      shopDomain,
      shopifyProductId: productId,
      rejectedByMerchant: false,
      confidenceTier: { in: ["CONFIRMED", "LIKELY"] },
    },
  });

  // Fetch matches for this product (top N by confidence)
  const matches = await db.productLevelMatch.findMany({
    where: {
      shopDomain,
      shopifyProductId: productId,
      rejectedByMerchant: false,
      confidenceTier: { in: ["CONFIRMED", "LIKELY"] },
    },
    include: {
      ScrapedProduct: {
        include: {
          ScrapedVariant: {
            orderBy: { updatedAt: "desc" },
            take: 1,
          },
        },
      },
    },
    orderBy: { confidence: "desc" },
    take: limit || undefined,
  });

  // Get ProductUrls for competitor links
  const scrapedIds = matches.map((m) => m.scrapedProductId);
  const urls = await db.productUrl.findMany({
    where: { prodId: { in: scrapedIds } },
    select: { prodId: true, url: true },
  });
  const urlByScraped = new Map(urls.map((u) => [u.prodId, u.url]));

  const matchData = matches.map((m) => {
    const scraped = m.ScrapedProduct;
    const variant = scraped?.ScrapedVariant[0];
    return {
      id: m.id,
      confidence: Number(m.confidence),
      confidenceTier: m.confidenceTier,
      source: m.source,
      confirmedByMerchant: m.confirmedByMerchant,
      scrapedTitle: scraped?.title ?? "(missing)",
      scrapedDomain: scraped?.domain ?? "",
      scrapedImageUrl: scraped?.imageUrl ?? null,
      competitorPrice: variant?.currentPrice?.toString() ?? null,
      competitorUrl: urlByScraped.get(m.scrapedProductId) ?? null,
      scrapedProductId: scraped?.id,
    };
  });

  return json({ matches: matchData, totalCount });
};
