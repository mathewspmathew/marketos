/**
 * app.stats._index.jsx
 *
 * Product picker for the per-product stats page. Lists dynamic-pricing-
 * enabled products with a quick "last decision" snippet so the merchant
 * can spot which ones are actively moving.
 */
import { Link, useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import db from "../db.server";
import { authenticate } from "../shopify.server";

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  const products = await db.shopifyProduct.findMany({
    where: { shopDomain, dynamicPricingEnabled: true },
    select: {
      id: true,
      title: true,
      imageUrl: true,
      pricingTier: true,
      basePrice: true,
      variants: { select: { currentPrice: true }, take: 1 },
    },
    orderBy: { lastDecisionAt: "desc" },
  });

  // One latest PriceDecision per product (via its first variant).
  const variantIds = products.flatMap((p) => p.variants.map((v) => v.currentPrice ? p.id : null)).filter(Boolean);
  const productIds = products.map((p) => p.id);
  const recent = productIds.length === 0 ? [] : await db.$queryRawUnsafe(`
    SELECT DISTINCT ON (v."productId")
           v."productId" AS pid, pd."changePct", pd."autoApplied",
           pd."skipReason", pd."decidedAt"
      FROM "PriceDecision" pd
      JOIN "ShopifyVariant" v ON v.id = pd."shopifyVariantId"
     WHERE v."productId" = ANY($1)
     ORDER BY v."productId", pd."decidedAt" DESC
  `, productIds);

  const recentByPid = new Map(recent.map((r) => [r.pid, r]));

  return {
    products: products.map((p) => {
      const r = recentByPid.get(p.id);
      return {
        id: p.id,
        title: p.title,
        imageUrl: p.imageUrl,
        tier: p.pricingTier,
        currentPrice: p.variants[0]?.currentPrice?.toString() ?? null,
        basePrice: p.basePrice?.toString() ?? null,
        lastDecisionAt: r?.decidedAt ? new Date(r.decidedAt).toISOString() : null,
        lastChangePct: r?.changePct ?? null,
        lastApplied:   r?.autoApplied ?? null,
        lastSkipReason: r?.skipReason ?? null,
      };
    }),
  };
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
                <Link to={`/app/stats/${encodeURIComponent(p.id)}`}
                      style={{ textDecoration: "none", color: "inherit", display: "block" }}>
                  <s-stack direction="inline" gap="base" align="center">
                    {p.imageUrl && (
                      <img src={p.imageUrl} alt={p.title} width="48" height="48"
                           style={{ objectFit: "cover", borderRadius: 4 }} />
                    )}
                    <s-stack direction="block" gap="tight">
                      <s-text emphasis="bold">{p.title}</s-text>
                      <s-text tone="subdued">
                        Tier {p.tier} · current ₹{p.currentPrice ?? "—"} · base ₹{p.basePrice ?? "—"}
                      </s-text>
                    </s-stack>
                    <s-stack direction="block" gap="tight">
                      {p.lastDecisionAt ? (
                        <>
                          <s-text>
                            Last: {new Date(p.lastDecisionAt).toLocaleString()}
                          </s-text>
                          <s-text tone={p.lastChangePct < 0 ? "critical" : "success"}>
                            {p.lastChangePct != null
                              ? `${(p.lastChangePct * 100).toFixed(2)}%`
                              : "no change"}
                            {p.lastApplied ? " · applied" : p.lastSkipReason ? ` · ${p.lastSkipReason}` : ""}
                          </s-text>
                        </>
                      ) : (
                        <s-text tone="subdued">No decisions yet</s-text>
                      )}
                    </s-stack>
                  </s-stack>
                </Link>
              </s-resource-item>
            ))}
          </s-resource-list>
        </s-section>
      )}
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
