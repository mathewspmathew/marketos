/**
 * app.history.$id.jsx — per-product price/competitor history timeline.
 * Charts merchant price decisions and competitor observations over the
 * trailing 30 days from raw DB reads (display-only aggregation). Match
 * lifecycle events (discovered/confirmed/rejected) come from Python's
 * product_stats.get_match_activity — the single source of truth also
 * used by app.stats.$productId.jsx, so both views agree on what an
 * "event" is.
 */
import { useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import db from "../db.server";
import { authenticate } from "../shopify.server";
import { getCurrencySymbol } from "../lib/currencyFormatter";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_HEADERS = { "X-Internal-Token": process.env.INTERNAL_API_TOKEN };

// 30-day window, capped to keep the chart cheap.
const WINDOW_DAYS = 30;
const MAX_POINTS  = 500;

export const loader = async ({ request, params }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain  = session.shop;
  const productId   = params.id;

  const product = await db.shopifyProduct.findFirst({
    where: { id: productId, shopDomain },
    include: { ShopifyVariant: true },
  });
  if (!product) {
    throw new Response("Product not found", { status: 404 });
  }

  const shopSettings = await db.shopSettings.findUnique({ where: { shopDomain } });

  const since = new Date(Date.now() - WINDOW_DAYS * 24 * 60 * 60 * 1000);
  const variantIds = product.ShopifyVariant.map((v) => v.id);

  // Merchant-side timeline: PriceDecisions across the product's variants.
  const decisions = variantIds.length
    ? await db.priceDecision.findMany({
        where: {
          shopDomain,
          shopifyVariantId: { in: variantIds },
          decidedAt: { gte: since },
        },
        orderBy: { decidedAt: "asc" },
        take: MAX_POINTS,
      })
    : [];

  // Competitor-side timeline: latest observations across matched competitor variants.
  const matches = await db.productLevelMatch.findMany({
    where: { shopDomain, shopifyProductId: productId, reviewStatus: { not: "REJECTED" } },
    include: {
      ScrapedProduct: {
        include: { ScrapedVariant: { select: { id: true } } },
      },
    },
  });
  const competitorVariantIds = matches.flatMap((m) => m.ScrapedProduct?.ScrapedVariant.map((v) => v.id) ?? []);
  const competitorByDomain = new Map(
    matches
      .filter((m) => m.ScrapedProduct)
      .flatMap((m) =>
        m.ScrapedProduct.ScrapedVariant.map((v) => [v.id, m.ScrapedProduct.domain])
      ),
  );

  const observations = competitorVariantIds.length
    ? await db.competitorPriceObservation.findMany({
        where: {
          shopDomain,
          competitorVariantId: { in: competitorVariantIds },
          observedAt: { gte: since },
        },
        orderBy: { observedAt: "asc" },
        take: MAX_POINTS,
      })
    : [];

  // Bucket competitor prices by domain so the chart legend is readable.
  const competitorSeries = new Map();
  for (const o of observations) {
    const dom = competitorByDomain.get(o.competitorVariantId) ?? "unknown";
    if (!competitorSeries.has(dom)) competitorSeries.set(dom, []);
    competitorSeries.get(dom).push({
      t: o.observedAt.toISOString(),
      price: Number(o.price),
    });
  }

  // Match activity events (created/confirmed/rejected) are synthesized by
  // product_stats.get_match_activity — Python-owned so it never drifts from
  // what app.stats.$productId.jsx's decision history considers an "event".
  let matchEvents = [];
  try {
    const resp = await fetch(
      `${PYTHON_API_URL}/internal/dynamic-pricing/match-activity` +
      `?shop_domain=${encodeURIComponent(shopDomain)}&product_id=${encodeURIComponent(productId)}&days=${WINDOW_DAYS}`,
      { headers: INTERNAL_HEADERS },
    );
    const json = await resp.json();
    if (json.ok) matchEvents = json.events;
  } catch (err) {
    console.error("[history] Failed to fetch match activity:", err);
  }

  return {
    product: { id: product.id, title: product.title, imageUrl: product.imageUrl },
    currency: shopSettings?.currency,
    decisions: decisions.map((d) => ({
      t: d.decidedAt.toISOString(),
      oldPrice: Number(d.oldPrice),
      newPrice: Number(d.newPrice),
      reason:   d.reason,
      applied:  Boolean(d.appliedAt),
    })),
    competitorSeries: [...competitorSeries.entries()].map(([domain, pts]) => ({ domain, pts })),
    matchActivity: matchEvents,
  };
};

// ── Tiny inline SVG sparkline. No chart library. ──────────────────────────────
function priceRange(decisions, competitorSeries) {
  const all = [
    ...decisions.flatMap((d) => [d.oldPrice, d.newPrice]),
    ...competitorSeries.flatMap((s) => s.pts.map((p) => p.price)),
  ].filter((n) => Number.isFinite(n) && n > 0);
  if (all.length === 0) return null;
  const min = Math.min(...all);
  const max = Math.max(...all);
  return { min: min * 0.95, max: max * 1.05 };
}

function timeRange(decisions, competitorSeries) {
  const times = [
    ...decisions.map((d) => +new Date(d.t)),
    ...competitorSeries.flatMap((s) => s.pts.map((p) => +new Date(p.t))),
  ];
  if (times.length === 0) return null;
  return { min: Math.min(...times), max: Math.max(...times) };
}

function PolyLine({ pts, width, height, tR, pR, stroke, dashed }) {
  if (!pts.length) return null;
  const path = pts.map((p, i) => {
    const x = ((+new Date(p.t) - tR.min) / Math.max(1, tR.max - tR.min)) * width;
    const y = height - ((p.price - pR.min) / Math.max(0.01, pR.max - pR.min)) * height;
    return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return (
    <path
      d={path}
      stroke={stroke}
      strokeWidth="1.5"
      strokeDasharray={dashed ? "4 3" : undefined}
      fill="none"
    />
  );
}

const COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6", "#6366f1"];

export default function HistoryPage() {
  const { product, currency, decisions, competitorSeries, matchActivity } = useLoaderData();
  const sym = getCurrencySymbol(currency);
  const pR = priceRange(decisions, competitorSeries);
  const tR = timeRange(decisions, competitorSeries);

  const merchantPts = decisions.map((d) => ({ t: d.t, price: d.newPrice }));
  const hasData = pR && tR && (decisions.length || competitorSeries.length);

  const W = 720;
  const H = 220;

  return (
    <s-page heading={`Price history: ${product.title}`}>
      <s-section heading="Last 30 days">
        {!hasData ? (
          <s-text tone="subdued">No price history yet. Enable Dynamic Pricing and wait for the first rescrape.</s-text>
        ) : (
          <s-stack direction="block" gap="base">
            <svg width={W} height={H} role="img" aria-label="Price history chart" style={{ background: "var(--s-color-bg-subdued, #f6f6f7)", borderRadius: 4 }}>
              {competitorSeries.map((s, i) => (
                <PolyLine key={s.domain} pts={s.pts} width={W} height={H} tR={tR} pR={pR} stroke={COLORS[i % COLORS.length]} />
              ))}
              <PolyLine pts={merchantPts} width={W} height={H} tR={tR} pR={pR} stroke="var(--s-color-text, #111)" dashed />
            </svg>

            <s-stack direction="inline" gap="large" wrap>
              <s-stack direction="inline" gap="small" alignItems="center">
                <span style={{ display: "inline-block", width: 14, height: 2, borderTop: "2px dashed var(--s-color-text, #111)" }} />
                <s-text>Your suggested price</s-text>
              </s-stack>
              {competitorSeries.map((s, i) => (
                <s-stack key={s.domain} direction="inline" gap="small" alignItems="center">
                  <span style={{ display: "inline-block", width: 14, height: 2, background: COLORS[i % COLORS.length] }} />
                  <s-text>{s.domain}</s-text>
                </s-stack>
              ))}
            </s-stack>
            <s-text tone="subdued">
              Range: {sym}{pR.min.toFixed(2)} — {sym}{pR.max.toFixed(2)}
            </s-text>
          </s-stack>
        )}
      </s-section>

      <s-section heading="Match activity">
        {matchActivity.length === 0 ? (
          <s-text tone="subdued">No match activity yet.</s-text>
        ) : (
          <s-stack direction="block" gap="small">
            {matchActivity.map((activity, idx) => (
              <s-box key={`${activity.matchId}-${idx}`} padding="base" borderWidth="base" borderRadius="base">
                <s-stack direction="inline" gap="base" justifyContent="space-between">
                  <s-stack direction="inline" gap="base" alignItems="start">
                    <s-text>
                      {activity.type === "confirmed" && "✓"}
                      {activity.type === "rejected" && "✕"}
                      {activity.type === "created" && "★"}
                    </s-text>
                    <s-stack direction="block" gap="small">
                      <s-text>{activity.description}</s-text>
                      <s-text tone="subdued">
                        {new Date(activity.timestamp).toLocaleString()}
                      </s-text>
                    </s-stack>
                  </s-stack>
                  <s-badge tone={activity.type === "confirmed" ? "success" : activity.type === "rejected" ? "critical" : "info"}>
                    {activity.type === "confirmed" ? "Confirmed" : activity.type === "rejected" ? "Rejected" : "Discovered"}
                  </s-badge>
                </s-stack>
              </s-box>
            ))}
          </s-stack>
        )}
      </s-section>

      <s-section heading="Decisions">
        {decisions.length === 0 ? (
          <s-text tone="subdued">No price decisions yet.</s-text>
        ) : (
          <s-table>
            <s-table-header-row>
              <s-table-header listSlot="primary">When</s-table-header>
              <s-table-header listSlot="labeled" format="currency">Old</s-table-header>
              <s-table-header listSlot="labeled" format="currency">New</s-table-header>
              <s-table-header listSlot="inline">Applied</s-table-header>
              <s-table-header listSlot="secondary">Reason</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {decisions.slice().reverse().map((d, i) => (
                <s-table-row key={i}>
                  <s-table-cell>{new Date(d.t).toLocaleString()}</s-table-cell>
                  <s-table-cell>{sym}{d.oldPrice.toFixed(2)}</s-table-cell>
                  <s-table-cell>{sym}{d.newPrice.toFixed(2)}</s-table-cell>
                  <s-table-cell>{d.applied ? "yes" : "no"}</s-table-cell>
                  <s-table-cell><s-text tone="subdued">{d.reason}</s-text></s-table-cell>
                </s-table-row>
              ))}
            </s-table-body>
          </s-table>
        )}
      </s-section>
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
