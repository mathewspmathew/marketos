import React from "react";
import { useFetcher, useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";
import { Page, Layout, Section, Box, Stack, Text } from "@shopify/polaris";

import db from "../db.server";
import { authenticate } from "../shopify.server";
import { MetricsSection } from "../components/matches/MetricsSection.jsx";
import { ProductMatchCard } from "../components/matches/ProductMatchCard.jsx";

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

  // Track expanded matches in local cache
  const [matchesCache, setMatchesCache] = React.useState({});
  const [pendingLoad, setPendingLoad] = React.useState(null);

  const act = (matchId, intent) =>
    fetcher.submit({ intent, matchId }, { method: "POST" });

  const loadMatches = (productId, limit = 3) => {
    if (matchesCache[productId]) return;
    setPendingLoad({ productId, limit });
    fetcher.load(`/app/matches.lazy?productId=${productId}&limit=${limit}`);
  };

  const loadAllMatches = (productId) => {
    if (matchesCache[`${productId}-all`]) return;
    setPendingLoad({ productId, limit: 999 });
    fetcher.load(`/app/matches.lazy?productId=${productId}&limit=999`);
  };

  // Cache fetched matches
  React.useEffect(() => {
    if (fetcher.data?.matches && fetcher.state === "idle" && pendingLoad) {
      const cacheKey = pendingLoad.limit === 999 ? `${pendingLoad.productId}-all` : pendingLoad.productId;
      setMatchesCache((prev) => ({
        ...prev,
        [cacheKey]: fetcher.data.matches,
      }));
      setPendingLoad(null);
    }
  }, [fetcher.data, fetcher.state, pendingLoad]);

  return (
    <Page title="Matched competitors">
      <Layout>
        <Layout.Section>
          <MetricsSection
            totalProducts={metrics.totalProducts}
            pendingReviews={metrics.pendingReviews}
            reviewPercentage={metrics.reviewPercentage}
            avgConfidence={metrics.avgConfidence}
          />
        </Layout.Section>

        <Layout.Section>
          {groups.length === 0 ? (
            <Section>
              <Box padding="500">
                <Text as="p" variant="headingMd" alignment="center">
                  No matches yet
                </Text>
                <Text as="p" tone="subdued" alignment="center">
                  Enable Dynamic Pricing on a product to start discovering competitors.
                </Text>
              </Box>
            </Section>
          ) : (
            <Stack gap="400">
              {groups.map((product) => (
                <ProductMatchCard
                  key={product.id}
                  product={product}
                  topMatch={product.topMatch}
                  onConfirm={(matchId) => act(matchId, "confirm")}
                  onReject={(matchId) => act(matchId, "reject")}
                  onLoadMore={(productId, limit) =>
                    limit === null ? loadAllMatches(productId) : loadMatches(productId, limit)
                  }
                  expandedMatches={
                    matchesCache[product.id]?.slice(0, 3) ||
                    matchesCache[`${product.id}-all`] ||
                    []
                  }
                  allMatchesCount={product.matchCount}
                  isLoading={fetcher.state === "loading"}
                  showAllMatches={!!matchesCache[`${product.id}-all`]}
                />
              ))}
            </Stack>
          )}
        </Layout.Section>
      </Layout>
    </Page>
  );
}

export const headers = (h) => boundary.headers(h);
