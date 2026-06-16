import { useFetcher, useLoaderData, useRouteError } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";
import { authenticate } from "../shopify.server";

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  // Fetch all non-rejected matches (CONFIRMED + LIKELY)
  const allMatches = await db.productLevelMatch.findMany({
    where: {
      shopDomain,
      rejectedByMerchant: false,
      confidenceTier: { in: ["CONFIRMED", "LIKELY"] },
    },
    include: {
      ShopifyProduct: {
        include: { ShopifyVariant: { take: 1 } },
      },
      ScrapedProduct: {
        include: {
          ScrapedVariant: {
            orderBy: { updatedAt: "desc" },
            take: 1,
          },
        },
      },
    },
    orderBy: [{ shopifyProductId: "asc" }, { confidence: "desc" }],
  });

  // Calculate metrics
  const uniqueProducts = new Set(allMatches.map((m) => m.shopifyProductId));
  const totalProducts = uniqueProducts.size;

  const unreviewed = allMatches.filter((m) => m.reviewedAt === null);
  const pendingReviews = unreviewed.length;

  const totalMatches = allMatches.length;
  const reviewedMatches = totalMatches - pendingReviews;
  const reviewPercentage = totalMatches > 0 ? Math.round((reviewedMatches / totalMatches) * 100) : 0;

  const confidenceSum = allMatches.reduce((sum, m) => sum + Number(m.confidence), 0);
  const avgConfidence = allMatches.length > 0 ? (confidenceSum / allMatches.length).toFixed(1) : "0.0";

  // Group by Shopify product, keep only TOP 1 match (highest confidence)
  const byProduct = new Map();
  for (const m of allMatches) {
    const sp = m.ShopifyProduct;
    if (!sp) continue;
    if (!byProduct.has(sp.id)) {
      byProduct.set(sp.id, {
        id: sp.id,
        title: sp.title,
        imageUrl: sp.imageUrl,
        merchantPrice: sp.ShopifyVariant[0]?.currentPrice?.toString() ?? null,
        matchCount: 0,
        topMatch: null,
      });
    }
    const product = byProduct.get(sp.id);
    product.matchCount += 1;

    // Store only top 1 match
    if (!product.topMatch) {
      const scraped = m.ScrapedProduct;
      const variant = scraped?.ScrapedVariant[0];
      product.topMatch = {
        id: m.id,
        confidence: Number(m.confidence),
        confidenceTier: m.confidenceTier,
        source: m.source,
        confirmedByMerchant: m.confirmedByMerchant,
        scrapedTitle: scraped?.title ?? "(missing)",
        scrapedDomain: scraped?.domain ?? "",
        scrapedImageUrl: scraped?.imageUrl ?? null,
        competitorPrice: variant?.currentPrice?.toString() ?? null,
        competitorUrl: null,
        scrapedProductId: scraped?.id,
      };
    }
  }

  // Pull ProductUrls for top 1 match in each product group
  const topScrapedIds = [...byProduct.values()].map((p) => p.topMatch?.scrapedProductId).filter(Boolean);
  if (topScrapedIds.length) {
    const urls = await db.productUrl.findMany({
      where: { prodId: { in: topScrapedIds } },
      select: { prodId: true, url: true },
    });
    const urlByScraped = new Map(urls.map((u) => [u.prodId, u.url]));
    for (const grp of byProduct.values()) {
      if (grp.topMatch) {
        grp.topMatch.competitorUrl = urlByScraped.get(grp.topMatch.scrapedProductId) ?? null;
      }
    }
  }

  return {
    metrics: {
      totalProducts,
      pendingReviews,
      reviewPercentage,
      avgConfidence: parseFloat(avgConfidence),
    },
    groups: [...byProduct.values()],
  };
};

export const action = async ({ request }) => {
  await authenticate.admin(request);
  const formData = await request.formData();
  const matchId = formData.get("matchId");
  const intent  = formData.get("intent");

  if (intent === "confirm") {
    await db.productLevelMatch.update({
      where: { id: matchId },
      data: { confirmedByMerchant: true, rejectedByMerchant: false, reviewedAt: new Date() },
    });
  } else if (intent === "reject") {
    await db.productLevelMatch.update({
      where: { id: matchId },
      data: { confirmedByMerchant: false, rejectedByMerchant: true, reviewedAt: new Date() },
    });
  }
  return null;
};

export default function MatchesPage() {
  const { metrics, groups } = useLoaderData();
  const fetcher = useFetcher();

  const act = (matchId, intent) =>
    fetcher.submit({ intent, matchId }, { method: "POST" });

  return (
    <s-page heading="Matched competitors" subheading={`${groups.length} product${groups.length === 1 ? "" : "s"} with matches`}>
      {groups.length === 0 ? (
        <s-section>
          <s-stack direction="block" gap="tight" align="center">
            <s-text emphasis="bold">No matches yet</s-text>
            <s-text tone="subdued">
              Enable Dynamic Pricing on a product to start discovering competitors.
            </s-text>
          </s-stack>
        </s-section>
      ) : (
        groups.map((g) => (
          <s-section key={g.id} heading={g.title}>
            <s-stack direction="inline" gap="base" align="center">
              {g.imageUrl && (
                <img src={g.imageUrl} alt={g.title} width="48" height="48" style={{ objectFit: "cover", borderRadius: 4 }} />
              )}
              {g.merchantPrice && <s-text>Your price: ₹{g.merchantPrice}</s-text>}
              <s-link href={`/app/product/${encodeURIComponent(g.id)}/activity`}>Match activity</s-link>
            </s-stack>

            <s-resource-list>
              {g.topMatch && (
                <s-resource-item key={g.topMatch.id} id={g.topMatch.id}>
                  {g.topMatch.scrapedImageUrl && (
                    <img slot="media" src={g.topMatch.scrapedImageUrl} alt={g.topMatch.scrapedTitle} width="50" height="50" style={{ objectFit: "cover", borderRadius: 4 }} />
                  )}
                  <s-stack direction="block" gap="tight">
                    <s-stack direction="inline" gap="base" align="center">
                      <s-text emphasis="bold">{g.topMatch.scrapedTitle}</s-text>
                      <s-badge>{g.topMatch.scrapedDomain}</s-badge>
                      <s-badge tone={g.topMatch.confidenceTier === "CONFIRMED" ? "success" : "info"}>
                        {g.topMatch.confidenceTier} ({(g.topMatch.confidence * 100).toFixed(0)}%)
                      </s-badge>
                      {g.topMatch.confirmedByMerchant && <s-badge tone="success">Confirmed</s-badge>}
                    </s-stack>
                    <s-stack direction="inline" gap="loose" align="center">
                      {g.topMatch.competitorPrice && <s-text>Their price: ₹{g.topMatch.competitorPrice}</s-text>}
                      {g.topMatch.competitorUrl && (
                        <s-link href={g.topMatch.competitorUrl} target="_blank">Open</s-link>
                      )}
                      {g.matchCount > 1 && (
                        <s-link href={`/app/product/${encodeURIComponent(g.id)}/all-matches`}>View all {g.matchCount} competitors</s-link>
                      )}
                    </s-stack>
                  </s-stack>
                </s-resource-item>
              )}
            </s-resource-list>
          </s-section>
        ))
      )}
    </s-page>
  );
}

export function ErrorBoundary() {
  return boundary.error(useRouteError());
}

export const headers = (h) => boundary.headers(h);
