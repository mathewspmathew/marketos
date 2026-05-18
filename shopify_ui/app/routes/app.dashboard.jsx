/* eslint-disable react/prop-types */
import { useLoaderData, useRouteError, Link } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

const MS_DAY = 86_400_000;

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const now = Date.now();
  const today1d  = new Date(now - 1  * MS_DAY);
  const day7  = new Date(now - 7  * MS_DAY);
  const day30 = new Date(now - 30 * MS_DAY);

  // Variant + product totals
  const totalVariants = await db.shopifyVariant.count({
    where: { product: { shopDomain: shop } },
  });

  const autoVariants = await db.shopifyVariant.count({
    where: { product: { shopDomain: shop }, autoPriceEnabled: true },
  });

  const totalProducts = await db.shopifyProduct.count({
    where: { shopDomain: shop },
  });

  // Products with at least one CONFIRMED match (eligibility gauge).
  const confirmedProductIds = await db.productLevelMatch.findMany({
    where: { shopDomain: shop, confidenceTier: "CONFIRMED" },
    distinct: ["shopifyProductId"],
    select: { shopifyProductId: true },
  });

  // Pending reviews
  const pendingReviews = await db.productLevelMatch.count({
    where: {
      shopDomain: shop,
      confidenceTier: "LIKELY",
      reviewedAt: null,
      rejectedByMerchant: false,
    },
  });

  // Price changes counters — sourced from applied PriceDecision rows
  // (PriceChange was folded into PriceDecision; appliedAt != NULL is the
  // signal that the decision was actually pushed to Shopify).
  const [changes1d, changes7d, changes30d] = await Promise.all([
    db.priceDecision.count({
      where: { shopDomain: shop, appliedAt: { gte: today1d } },
    }),
    db.priceDecision.count({
      where: { shopDomain: shop, appliedAt: { gte: day7 } },
    }),
    db.priceDecision.count({
      where: { shopDomain: shop, appliedAt: { gte: day30 } },
    }),
  ]);

  const impactRows = await db.$queryRaw`
    SELECT pd."shopifyVariantId" AS variant_id,
           SUM(pd."newPrice" - pd."oldPrice") AS net_delta
    FROM "PriceDecision" pd
    WHERE pd."shopDomain" = ${shop}
      AND pd."appliedAt" >= ${day30}
      AND pd."revertedAt" IS NULL
    GROUP BY pd."shopifyVariantId"
  `;

  const variantSales = await db.salesAggregate.findMany({
    where: {
      shopifyVariantId: { in: impactRows.map((r) => r.variant_id) },
    },
    select: { shopifyVariantId: true, orders28d: true },
  });
  const salesMap = Object.fromEntries(
    variantSales.map((s) => [s.shopifyVariantId, s.orders28d || 0]),
  );

  let estRevenue = 0;
  for (const r of impactRows) {
    const orders = salesMap[r.variant_id] || 0;
    estRevenue += Number(r.net_delta) * orders;
  }

  // Recent activity feed: last 10 applied decisions
  const recent = await db.priceDecision.findMany({
    where: { shopDomain: shop, appliedAt: { not: null } },
    orderBy: { appliedAt: "desc" },
    take: 10,
    include: {
      variant: {
        select: {
          id: true, title: true,
          product: { select: { title: true } },
        },
      },
    },
  });

  // Open alerts — folded from PricingAlert. A decision is "alerting" when
  // alertReason != NULL and alertDismissedAt is NULL.
  const criticalOpen = await db.priceDecision.count({
    where: { shopDomain: shop, alertDismissedAt: null, alertSeverity: "CRITICAL" },
  });
  const warnOpen = await db.priceDecision.count({
    where: { shopDomain: shop, alertDismissedAt: null, alertSeverity: "WARN" },
  });

  return {
    shop,
    kpis: {
      totalVariants,
      autoVariants,
      eligibleProducts: confirmedProductIds.length,
      totalProducts,
      pendingReviews,
      changes1d, changes7d, changes30d,
      estRevenue,
      criticalOpen, warnOpen,
    },
    recent: recent.map((c) => ({
      id:        c.id,
      appliedAt: c.appliedAt,
      oldPrice:  Number(c.oldPrice),
      newPrice:  Number(c.newPrice),
      reverted:  !!c.revertedAt,
      variantId: c.variant?.id,
      title:     c.variant ? `${c.variant.product.title} — ${c.variant.title}` : "(deleted)",
    })),
  };
};

export default function DashboardPage() {
  const { kpis, recent } = useLoaderData();
  const coverage = kpis.totalProducts > 0
    ? Math.round((kpis.eligibleProducts / kpis.totalProducts) * 100)
    : 0;

  return (
    <s-page heading="Pricing dashboard">
      <s-section>
        <s-stack direction="inline" gap="base">
          <Kpi label="Price changes today"   value={kpis.changes1d} />
          <Kpi label="Last 7 days"           value={kpis.changes7d} />
          <Kpi label="Last 30 days"          value={kpis.changes30d} />
          <Kpi label="Est. revenue impact (30d)"
               value={`₹${kpis.estRevenue.toFixed(0)}`}
               tone={kpis.estRevenue >= 0 ? "success" : "critical"} />
        </s-stack>
      </s-section>

      <s-section heading="Coverage">
        <s-stack direction="inline" gap="base">
          <Kpi label="Auto-pricing variants"
               value={`${kpis.autoVariants} / ${kpis.totalVariants}`} />
          <Kpi label="CONFIRMED product coverage"
               value={`${kpis.eligibleProducts} / ${kpis.totalProducts} (${coverage}%)`} />
          <Kpi label="Pending review"
               value={kpis.pendingReviews}
               link="/app/approve"
               tone={kpis.pendingReviews > 0 ? "warn" : "subdued"} />
        </s-stack>
      </s-section>

      <s-section heading="Alerts">
        <s-stack direction="inline" gap="base">
          <Kpi label="Open CRITICAL"
               value={kpis.criticalOpen}
               link="/app/alerts?severity=CRITICAL"
               tone={kpis.criticalOpen > 0 ? "critical" : "subdued"} />
          <Kpi label="Open WARN"
               value={kpis.warnOpen}
               link="/app/alerts?severity=WARN"
               tone={kpis.warnOpen > 0 ? "warn" : "subdued"} />
        </s-stack>
      </s-section>

      <s-section heading="Recent price changes">
        {recent.length === 0 ? (
          <s-text tone="subdued">No price changes yet.</s-text>
        ) : (
          <s-stack gap="tight">
            {recent.map((r) => {
              const delta = r.newPrice - r.oldPrice;
              const arrow = delta > 0 ? "↑" : delta < 0 ? "↓" : "·";
              return (
                <s-stack key={r.id} direction="inline" gap="base" alignment="space-between"
                  style={{ padding: 8, borderRadius: 6, border: "1px solid #e4e5e7",
                           background: r.reverted ? "#f6f6f7" : "transparent" }}>
                  <s-text>
                    <span style={{ color: delta < 0 ? "#0a5a2a" : "#bf0711" }}>{arrow}</span>
                    {" "}₹{r.oldPrice.toFixed(2)} → ₹{r.newPrice.toFixed(2)}
                    {r.reverted ? "  (reverted)" : ""}
                  </s-text>
                  <s-text>
                    {r.variantId ? (
                      <Link to={`/app/pricing/${encodeURIComponent(r.variantId)}`}>{r.title}</Link>
                    ) : r.title}
                  </s-text>
                  <s-text tone="subdued">{new Date(r.appliedAt).toLocaleString()}</s-text>
                </s-stack>
              );
            })}
          </s-stack>
        )}
      </s-section>
    </s-page>
  );
}

function Kpi({ label, value, tone, link }) {
  const fg =
    tone === "success"  ? "#0a5a2a" :
    tone === "critical" ? "#bf0711" :
    tone === "warn"     ? "#b9852c" : "#0a0a0a";
  const card = (
    <div style={{ flex: 1, padding: 12, borderRadius: 8, border: "1px solid #e4e5e7" }}>
      <div style={{ fontSize: 12, color: "#6d7175", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: fg }}>{value}</div>
    </div>
  );
  return link ? <Link to={link} style={{ textDecoration: "none", flex: 1 }}>{card}</Link> : card;
}

export function ErrorBoundary() {
  return boundary.error(useRouteError());
}
