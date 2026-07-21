/**
 * app.stats._index.jsx
 *
 * Product picker for the per-product stats page. Lists dynamic-pricing-
 * enabled products with a quick "last decision" snippet so the merchant
 * can spot which ones are actively moving. Pure presentation layer — the
 * list and its computed lastStatus come from
 * services/pricing_svc/product_stats.py::list_product_stats.
 */
import { Link, useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  const res = await fetch(
    `${PYTHON_API_URL}/internal/dynamic-pricing/products-stats-list?shop_domain=${encodeURIComponent(shopDomain)}`,
  );
  const data = await res.json();
  if (!data.ok) throw new Response(data.error || "Failed to load stats", { status: 500 });

  return { products: data.products };
};

export default function StatsIndex() {
  const { products } = useLoaderData();

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
        <s-section>
          <s-resource-list>
            {products.map((p) => (
              <s-resource-item key={p.id} id={p.id}>
                <s-stack direction="block" gap="tight" style={{ width: "100%" }}>
                  <Link to={`/app/stats/${encodeURIComponent(p.id)}`}
                        style={{ textDecoration: "none", color: "inherit", display: "block" }}>
                    <s-stack direction="inline" gap="base" align="center">
                      {p.imageUrl && (
                        <img src={p.imageUrl} alt={p.title} width="48" height="48"
                             style={{ objectFit: "cover", borderRadius: 4 }} />
                      )}
                      <s-stack direction="block" gap="tight" style={{ flex: 1 }}>
                        <s-text emphasis="bold">{p.title}</s-text>
                        <s-text tone="subdued">
                          Tier {p.tier} · current ₹{p.currentPrice != null ? Number(p.currentPrice).toFixed(2) : "—"} · base ₹{p.avgBasePrice != null ? Number(p.avgBasePrice).toFixed(2) : "—"}
                        </s-text>
                      </s-stack>
                    </s-stack>
                  </Link>
                  {p.lastDecisionAt ? (
                    <s-stack direction="inline" gap="tight" align="center">
                      <s-text>
                        Last: {new Date(p.lastDecisionAt).toLocaleString()}
                      </s-text>
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
                </s-stack>
              </s-resource-item>
            ))}
          </s-resource-list>
        </s-section>
      )}
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
