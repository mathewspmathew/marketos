/**
 * app.matches.jsx — "Matched competitors" review page. Groups matched
 * competitor products by merchant product (one top match + expandable
 * list per product, fetched lazily via app.matches.lazy.jsx). All grouping/
 * metrics computation lives in services/pricing_svc/product_stats.py
 * (list_matches_for_review) so the browser and a future chatbot tool see
 * identical numbers; confirm/reject actions are proxied to
 * services/pricing_svc/match_review.py via /internal/matches/review.
 */
import React from "react";
import { useFetcher, useLoaderData, useRouteError } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  const res = await fetch(
    `${PYTHON_API_URL}/internal/dynamic-pricing/matches?shop_domain=${encodeURIComponent(shopDomain)}`,
  );
  const data = await res.json();
  if (!data.ok) throw new Response(data.error ?? "Failed to load matches.", { status: 502 });

  return { metrics: data.metrics, groups: data.groups };
};

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const formData = await request.formData();
  const matchId = formData.get("matchId");
  const intent = formData.get("intent");

  if (intent !== "confirm" && intent !== "reject") return { ok: false, error: "unknown_intent" };

  const res = await fetch(`${PYTHON_API_URL}/internal/matches/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ shop_domain: shop, match_id: matchId, action: intent }),
  });
  const data = await res.json();
  if (!data.ok) return { ok: false, error: data.error };
  return { ok: true };
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
      <s-section padding="base">
        <s-grid
          gridTemplateColumns="@container (inline-size <= 400px) 1fr, 1fr auto 1fr auto 1fr auto 1fr"
          gap="small"
        >
          <s-box paddingBlock="small-400" paddingInline="small-100">
            <s-grid gap="small-300">
              <s-heading>Products with matches</s-heading>
              <s-text>{metrics.totalProducts}</s-text>
            </s-grid>
          </s-box>
          <s-divider direction="block"></s-divider>
          <s-box paddingBlock="small-400" paddingInline="small-100">
            <s-grid gap="small-300">
              <s-heading>Pending review</s-heading>
              <s-text tone={metrics.pendingReviews > 0 ? "critical" : undefined}>
                {metrics.pendingReviews}
              </s-text>
            </s-grid>
          </s-box>
          <s-divider direction="block"></s-divider>
          <s-box paddingBlock="small-400" paddingInline="small-100">
            <s-grid gap="small-300">
              <s-heading>Review completion</s-heading>
              <s-text tone={metrics.reviewPercentage >= 80 ? "success" : undefined}>
                {metrics.reviewPercentage}%
              </s-text>
            </s-grid>
          </s-box>
          <s-divider direction="block"></s-divider>
          <s-box paddingBlock="small-400" paddingInline="small-100">
            <s-grid gap="small-300">
              <s-heading>Avg confidence</s-heading>
              <s-text>{metrics.avgConfidence}</s-text>
            </s-grid>
          </s-box>
        </s-grid>
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
              <s-stack direction="block" gap="tight" style={{ flexGrow: 1 }}>
                <s-text>{product.merchantPrice ? `Your price: ₹${product.merchantPrice}` : "Price not set"}</s-text>
                <s-text tone="subdued">{product.matchCount} competitor{product.matchCount !== 1 ? "s" : ""} found</s-text>
              </s-stack>
              <s-button size="slim" onClick={() => toggleExpand(product.id)} variant={expandedProducts[product.id] ? "primary" : "plain"}>
                {expandedProducts[product.id] ? "Collapse" : "Expand"}
              </s-button>
              <s-link href={`/app/history/${encodeURIComponent(product.id)}`}>Activity</s-link>
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
                      {product.topMatch.reviewStatus === "CONFIRMED" && <s-badge tone="success">Confirmed</s-badge>}
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
                        {m.confidenceTier === "LIKELY" && m.reviewStatus === "PENDING" && (
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
                        {m.confidenceTier === "LIKELY" && m.reviewStatus === "PENDING" && (
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
