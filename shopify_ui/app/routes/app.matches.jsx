import React from "react";
import { useFetcher, useLoaderData, useRouteError } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import db from "../db.server";
import { authenticate } from "../shopify.server";

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

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

  // Metrics
  const uniqueProducts = new Set(allMatches.map((m) => m.shopifyProductId));
  const totalProducts = uniqueProducts.size;
  const unreviewed = allMatches.filter((m) => m.reviewedAt === null);
  const pendingReviews = unreviewed.length;
  const totalMatches = allMatches.length;
  const reviewedMatches = totalMatches - pendingReviews;
  const reviewPercentage = totalMatches > 0 ? Math.round((reviewedMatches / totalMatches) * 100) : 0;
  const confidenceSum = allMatches.reduce((sum, m) => sum + Number(m.confidence), 0);
  const avgConfidence = allMatches.length > 0 ? (confidenceSum / allMatches.length).toFixed(1) : "0.0";

  // Group by Shopify product with top 1 match
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
  const intent = formData.get("intent");

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
  const [expandedProducts, setExpandedProducts] = React.useState({});
  const [matchesCache, setMatchesCache] = React.useState({});
  const [pendingLoad, setPendingLoad] = React.useState(null);

  const act = (matchId, intent) => fetcher.submit({ intent, matchId }, { method: "POST" });

  const toggleExpand = (productId) => {
    setExpandedProducts((prev) => ({ ...prev, [productId]: !prev[productId] }));
    if (!expandedProducts[productId] && !matchesCache[productId]) {
      loadMatches(productId, 3);
    }
  };

  const loadMatches = (productId, limit) => {
    if (matchesCache[productId]) return;
    setPendingLoad({ productId, limit });
    fetcher.load(`/app/matches/lazy?productId=${productId}&limit=${limit}`);
  };

  const loadAllMatches = (productId) => {
    if (matchesCache[`${productId}-all`]) return;
    setPendingLoad({ productId, limit: 999 });
    fetcher.load(`/app/matches/lazy?productId=${productId}&limit=999`);
  };

  React.useEffect(() => {
    if (fetcher.data?.matches && fetcher.state === "idle" && pendingLoad) {
      const cacheKey = pendingLoad.limit === 999 ? `${pendingLoad.productId}-all` : pendingLoad.productId;
      setMatchesCache((prev) => ({ ...prev, [cacheKey]: fetcher.data.matches }));
      setPendingLoad(null);
    }
  }, [fetcher.data, fetcher.state, pendingLoad]);

  return (
    <s-page heading="Matched competitors" subheading={`${metrics.totalProducts} product${metrics.totalProducts === 1 ? "" : "s"}`}>
      {/* Metrics Section */}
      <s-section>
        <s-stack direction="inline" gap="loose" wrap>
          <div style={{ flex: 1, padding: "12px", borderRadius: "8px", border: "1px solid #e4e5e7" }}>
            <div style={{ fontSize: "12px", color: "#6d7175", marginBottom: "4px" }}>Products with matches</div>
            <div style={{ fontSize: "22px", fontWeight: "700" }}>{metrics.totalProducts}</div>
          </div>
          <div style={{ flex: 1, padding: "12px", borderRadius: "8px", border: "1px solid #e4e5e7" }}>
            <div style={{ fontSize: "12px", color: "#6d7175", marginBottom: "4px" }}>Pending review</div>
            <div style={{ fontSize: "22px", fontWeight: "700", color: metrics.pendingReviews > 0 ? "#bf0711" : "#0a0a0a" }}>{metrics.pendingReviews}</div>
          </div>
          <div style={{ flex: 1, padding: "12px", borderRadius: "8px", border: "1px solid #e4e5e7" }}>
            <div style={{ fontSize: "12px", color: "#6d7175", marginBottom: "4px" }}>Review completion</div>
            <div style={{ fontSize: "22px", fontWeight: "700", color: metrics.reviewPercentage >= 80 ? "#0a5a2a" : "#0a0a0a" }}>{metrics.reviewPercentage}%</div>
          </div>
          <div style={{ flex: 1, padding: "12px", borderRadius: "8px", border: "1px solid #e4e5e7" }}>
            <div style={{ fontSize: "12px", color: "#6d7175", marginBottom: "4px" }}>Avg confidence</div>
            <div style={{ fontSize: "22px", fontWeight: "700" }}>{metrics.avgConfidence}</div>
          </div>
        </s-stack>
      </s-section>

      {/* Products Section */}
      {groups.length === 0 ? (
        <s-section>
          <s-stack direction="block" gap="tight" align="center">
            <s-text emphasis="bold">No matches yet</s-text>
            <s-text tone="subdued">Enable Dynamic Pricing on a product to start discovering competitors.</s-text>
          </s-stack>
        </s-section>
      ) : (
        groups.map((product) => (
          <s-section key={product.id} heading={product.title}>
            {/* Product Header */}
            <s-stack direction="inline" gap="base" align="center" wrap>
              {product.imageUrl && <img src={product.imageUrl} alt={product.title} width="48" height="48" style={{ objectFit: "cover", borderRadius: 4 }} />}
              <div style={{ flex: 1 }}>
                <s-text>{product.merchantPrice ? `Your price: ₹${product.merchantPrice}` : "Price not set"}</s-text>
                <s-text tone="subdued">{product.matchCount} competitor{product.matchCount !== 1 ? "s" : ""} found</s-text>
              </div>
              <s-button size="slim" onClick={() => toggleExpand(product.id)} variant={expandedProducts[product.id] ? "primary" : "plain"}>
                {expandedProducts[product.id] ? "Collapse" : "Expand"}
              </s-button>
              <s-link href={`/app/product/${encodeURIComponent(product.id)}/activity`}>Activity</s-link>
            </s-stack>

            {/* Top Match (always shown) */}
            {product.topMatch && (
              <s-resource-list>
                <s-resource-item key={product.topMatch.id}>
                  {product.topMatch.scrapedImageUrl && (
                    <img slot="media" src={product.topMatch.scrapedImageUrl} alt={product.topMatch.scrapedTitle} width="50" height="50" style={{ objectFit: "cover", borderRadius: 4 }} />
                  )}
                  <s-stack direction="block" gap="tight">
                    <s-stack direction="inline" gap="base" align="center">
                      <s-text emphasis="bold">{product.topMatch.scrapedTitle}</s-text>
                      <s-badge>{product.topMatch.scrapedDomain}</s-badge>
                      <s-badge tone={product.topMatch.confidenceTier === "CONFIRMED" ? "success" : "info"}>
                        {product.topMatch.confidenceTier} ({(product.topMatch.confidence * 100).toFixed(0)}%)
                      </s-badge>
                      {product.topMatch.confirmedByMerchant && <s-badge tone="success">Confirmed</s-badge>}
                    </s-stack>
                    <s-stack direction="inline" gap="loose" align="center">
                      {product.topMatch.competitorPrice && <s-text>Their price: ₹{product.topMatch.competitorPrice}</s-text>}
                      {product.topMatch.competitorUrl && <s-link href={product.topMatch.competitorUrl} target="_blank">Open</s-link>}
                    </s-stack>
                  </s-stack>
                </s-resource-item>
              </s-resource-list>
            )}

            {/* Expanded Matches (Top 3) */}
            {expandedProducts[product.id] && matchesCache[product.id] && (
              <s-resource-list>
                {matchesCache[product.id].slice(0, 3).map((m) => (
                  <s-resource-item key={m.id} id={m.id}>
                    {m.scrapedImageUrl && (
                      <img slot="media" src={m.scrapedImageUrl} alt={m.scrapedTitle} width="50" height="50" style={{ objectFit: "cover", borderRadius: 4 }} />
                    )}
                    <s-stack direction="block" gap="tight">
                      <s-stack direction="inline" gap="base" align="center">
                        <s-text emphasis="bold">{m.scrapedTitle}</s-text>
                        <s-badge>{m.scrapedDomain}</s-badge>
                        <s-badge tone={m.confidenceTier === "CONFIRMED" ? "success" : "info"}>
                          {m.confidenceTier} ({(m.confidence * 100).toFixed(0)}%)
                        </s-badge>
                      </s-stack>
                      <s-stack direction="inline" gap="loose" align="center">
                        {m.competitorPrice && <s-text>₹{m.competitorPrice}</s-text>}
                        {m.competitorUrl && <s-link href={m.competitorUrl} target="_blank">Open</s-link>}
                        {m.confidenceTier === "LIKELY" && !m.confirmedByMerchant && (
                          <>
                            <s-button size="slim" onClick={() => act(m.id, "confirm")}>Confirm</s-button>
                            <s-button size="slim" variant="plain" onClick={() => act(m.id, "reject")}>Reject</s-button>
                          </>
                        )}
                      </s-stack>
                    </s-stack>
                  </s-resource-item>
                ))}
              </s-resource-list>
            )}

            {/* View All Button */}
            {expandedProducts[product.id] && product.matchCount > 3 && !matchesCache[`${product.id}-all`] && (
              <s-stack direction="inline" gap="base">
                <s-button onClick={() => loadAllMatches(product.id)}>View all {product.matchCount} competitors</s-button>
              </s-stack>
            )}

            {/* All Matches */}
            {expandedProducts[product.id] && matchesCache[`${product.id}-all`] && (
              <s-resource-list>
                {matchesCache[`${product.id}-all`].map((m) => (
                  <s-resource-item key={m.id} id={m.id}>
                    {m.scrapedImageUrl && (
                      <img slot="media" src={m.scrapedImageUrl} alt={m.scrapedTitle} width="50" height="50" style={{ objectFit: "cover", borderRadius: 4 }} />
                    )}
                    <s-stack direction="block" gap="tight">
                      <s-stack direction="inline" gap="base" align="center">
                        <s-text emphasis="bold">{m.scrapedTitle}</s-text>
                        <s-badge>{m.scrapedDomain}</s-badge>
                        <s-badge tone={m.confidenceTier === "CONFIRMED" ? "success" : "info"}>
                          {m.confidenceTier} ({(m.confidence * 100).toFixed(0)}%)
                        </s-badge>
                      </s-stack>
                      <s-stack direction="inline" gap="loose" align="center">
                        {m.competitorPrice && <s-text>₹{m.competitorPrice}</s-text>}
                        {m.competitorUrl && <s-link href={m.competitorUrl} target="_blank">Open</s-link>}
                        {m.confidenceTier === "LIKELY" && !m.confirmedByMerchant && (
                          <>
                            <s-button size="slim" onClick={() => act(m.id, "confirm")}>Confirm</s-button>
                            <s-button size="slim" variant="plain" onClick={() => act(m.id, "reject")}>Reject</s-button>
                          </>
                        )}
                      </s-stack>
                    </s-stack>
                  </s-resource-item>
                ))}
              </s-resource-list>
            )}
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
