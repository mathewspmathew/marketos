/**
 * app.stats._index.jsx
 *
 * Product picker for the per-product stats page. Lists dynamic-pricing-
 * enabled products with a quick "last decision" snippet so the merchant
 * can spot which ones are actively moving. Pure presentation layer — the
 * list and its computed lastStatus come from
 * services/pricing_svc/product_stats.py::list_product_stats.
 */
import React from "react";
import { Link, useLoaderData, useNavigate, useNavigation, useRevalidator } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";
import { subscribeToEventStream } from "../lib/eventStream";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_HEADERS = { "X-Internal-Token": process.env.INTERNAL_API_TOKEN };

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  const res = await fetch(
    `${PYTHON_API_URL}/internal/dynamic-pricing/products-stats-list?shop_domain=${encodeURIComponent(shopDomain)}`,
    { headers: INTERNAL_HEADERS },
  );
  const data = await res.json();
  if (!data.ok) throw new Response(data.error || "Failed to load stats", { status: 500 });

  return { products: data.products };
};

export default function StatsIndex() {
  const { products } = useLoaderData();
  const revalidator = useRevalidator();
  const navigation = useNavigation();
  const navigate = useNavigate();

  // Which product (if any) is being navigated to right now — drives the
  // pending-row spinner below so a click gives instant feedback even when
  // prefetch="intent" didn't get a chance to run (keyboard nav, fast click).
  const pendingMatch = navigation.location?.pathname.match(/^\/app\/stats\/([^/]+)$/);
  const pendingProductId = pendingMatch ? decodeURIComponent(pendingMatch[1]) : null;

  // Live updates: any pricing decision written for this shop (decide.py)
  // refreshes this list's lastDecisionAt/lastStatus without a manual reload.
  React.useEffect(() => {
    return subscribeToEventStream("/app/stats/stream", "stats_updated", () => {
      revalidator.revalidate();
    });
  }, [revalidator]);

  React.useEffect(() => {
    const t = setInterval(() => revalidator.revalidate(), 60_000);
    return () => clearInterval(t);
  }, [revalidator]);

  return (
    <s-page heading="Stats">
      <s-section>
        <s-text tone="subdued">
          Click a product to see its decision history and competitor price chart.
        </s-text>
      </s-section>

      {products.length === 0 ? (
        <s-section>
          <s-text>No products have dynamic pricing turned on yet.</s-text>
        </s-section>
      ) : (
        <s-section padding="none">
          <s-table>
            <s-table-header-row>
              <s-table-header listSlot="primary">Product</s-table-header>
              <s-table-header listSlot="inline">Tier</s-table-header>
              <s-table-header listSlot="labeled" format="currency">Current price</s-table-header>
              <s-table-header listSlot="secondary">Last decision</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {products.map((p) => {
                const isPending = p.id === pendingProductId;
                return (
                <s-table-row
                  key={p.id}
                  onClick={() => navigate(`/app/stats/${encodeURIComponent(p.id)}`)}
                  style={{ cursor: "pointer" }}
                >
                  <s-table-cell>
                    <s-stack direction="inline" gap="base" alignItems="center">
                      {p.imageUrl && (
                        <img src={p.imageUrl} alt="" width="32" height="32"
                             style={{ objectFit: "cover", borderRadius: 4, opacity: isPending ? 0.5 : 1 }} />
                      )}
                      <Link
                        to={`/app/stats/${encodeURIComponent(p.id)}`}
                        prefetch="intent"
                        onClick={(e) => e.stopPropagation()}
                        style={{ textDecoration: "none", color: "inherit" }}
                      >
                        <s-text emphasis="bold">{p.title}</s-text>
                      </Link>
                      {isPending && <s-spinner size="small" accessibilityLabel="Loading" />}
                    </s-stack>
                  </s-table-cell>
                  <s-table-cell>{p.tier}</s-table-cell>
                  <s-table-cell>
                    <s-stack direction="block" gap="small">
                      <s-text>
                        {p.currentPrice != null ? `₹${Number(p.currentPrice).toFixed(2)}` : "—"}
                      </s-text>
                      {p.avgBasePrice != null && (
                        <s-text tone="subdued" type="small">
                          Base ₹{Number(p.avgBasePrice).toFixed(2)}
                        </s-text>
                      )}
                    </s-stack>
                  </s-table-cell>
                  <s-table-cell>
                    {p.lastDecisionAt ? (
                      <s-stack direction="inline" gap="small" alignItems="center">
                        <s-text>{new Date(p.lastDecisionAt).toLocaleString()}</s-text>
                        <s-text tone={p.lastChangePct < 0 ? "critical" : "success"}>
                          {p.lastChangePct != null
                            ? `${(p.lastChangePct * 100).toFixed(2)}%`
                            : "no change"}
                        </s-text>
                        {p.lastStatus === "applied"  && <s-badge tone="success">Applied</s-badge>}
                        {p.lastStatus === "failed"   && <s-badge tone="critical">Push failed</s-badge>}
                        {p.lastStatus === "pending"  && <s-badge tone="warning">Pending push</s-badge>}
                        {p.lastStatus === "skipped"  && <s-badge tone="subdued">Skipped</s-badge>}
                      </s-stack>
                    ) : (
                      <s-text tone="subdued">No decisions yet</s-text>
                    )}
                  </s-table-cell>
                </s-table-row>
                );
              })}
            </s-table-body>
          </s-table>
        </s-section>
      )}
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
