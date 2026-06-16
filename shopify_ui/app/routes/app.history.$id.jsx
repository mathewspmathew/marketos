import { useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import db from "../db.server";
import { authenticate } from "../shopify.server";
import { MatchActivitySection } from "../components/history/MatchActivitySection.jsx";

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
    where: { shopDomain, shopifyProductId: productId, rejectedByMerchant: false },
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

  // Match activity for this product
  const matchActivity = await db.productLevelMatch.findMany({
    where: {
      shopDomain,
      shopifyProductId: productId,
      rejectedByMerchant: false,
    },
    include: {
      ScrapedProduct: { select: { title: true, domain: true } },
    },
    orderBy: { createdAt: "desc" },
  });

  // Convert matches to activity events
  const matchEvents = matchActivity.flatMap((m) => {
    const events = [];
    if (m.createdAt) {
      events.push({
        matchId: m.id,
        type: "created",
        timestamp: m.createdAt,
        description: `New competitor discovered: ${m.ScrapedProduct?.title} (${m.ScrapedProduct?.domain})`,
      });
    }
    if (m.confirmedByMerchant && m.reviewedAt) {
      events.push({
        matchId: m.id,
        type: "confirmed",
        timestamp: m.reviewedAt,
        description: `Confirmed match: ${m.ScrapedProduct?.title}`,
      });
    }
    if (m.rejectedByMerchant && m.reviewedAt) {
      events.push({
        matchId: m.id,
        type: "rejected",
        timestamp: m.reviewedAt,
        description: `Rejected match: ${m.ScrapedProduct?.title}`,
      });
    }
    return events;
  });

  // Sort all events by timestamp desc
  matchEvents.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));

  return {
    product: { id: product.id, title: product.title, imageUrl: product.imageUrl },
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
  const { product, decisions, competitorSeries, matchActivity } = useLoaderData();
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
              <PolyLine pts={merchantPts} width={W} height={H} tR={tR} pR={pR} stroke="#111" dashed />
            </svg>

            <s-stack direction="inline" gap="loose" wrap>
              <s-stack direction="inline" gap="tight" align="center">
                <span style={{ display: "inline-block", width: 14, height: 2, borderTop: "2px dashed #111" }} />
                <s-text>Your suggested price</s-text>
              </s-stack>
              {competitorSeries.map((s, i) => (
                <s-stack key={s.domain} direction="inline" gap="tight" align="center">
                  <span style={{ display: "inline-block", width: 14, height: 2, background: COLORS[i % COLORS.length] }} />
                  <s-text>{s.domain}</s-text>
                </s-stack>
              ))}
            </s-stack>
            <s-text tone="subdued">
              Range: ${pR.min.toFixed(2)} — ${pR.max.toFixed(2)}
            </s-text>
          </s-stack>
        )}
      </s-section>

      <s-section heading="Match activity">
        <MatchActivitySection activities={matchActivity} />
      </s-section>

      <s-section heading="Decisions">
        {decisions.length === 0 ? (
          <s-text tone="subdued">No price decisions yet.</s-text>
        ) : (
          <s-table>
            <s-table-header-row>
              <s-table-header>When</s-table-header>
              <s-table-header>Old</s-table-header>
              <s-table-header>New</s-table-header>
              <s-table-header>Applied</s-table-header>
              <s-table-header>Reason</s-table-header>
            </s-table-header-row>
            {decisions.slice().reverse().map((d, i) => (
              <s-table-row key={i}>
                <s-table-cell>{new Date(d.t).toLocaleString()}</s-table-cell>
                <s-table-cell>${d.oldPrice.toFixed(2)}</s-table-cell>
                <s-table-cell>${d.newPrice.toFixed(2)}</s-table-cell>
                <s-table-cell>{d.applied ? "yes" : "no"}</s-table-cell>
                <s-table-cell><s-text tone="subdued">{d.reason}</s-text></s-table-cell>
              </s-table-row>
            ))}
          </s-table>
        )}
      </s-section>
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
