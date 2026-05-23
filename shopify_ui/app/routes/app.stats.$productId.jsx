/**
 * app.stats.$productId.jsx
 *
 * Per-product auto-pricing stats. Two panels:
 *   1. Decision history — every PriceDecision row (applied or skipped),
 *      so the merchant can see WHY a change happened (or didn't).
 *   2. Competitor price chart — last 30 days of merchant variant price +
 *      the top-K competitor variant prices on the same axis.
 *
 * The chart is deliberately rendered as plain SVG (no chart library) so
 * the page stays simple to iterate on. Swap for a richer lib later.
 */
import { useLoaderData, Link } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import db from "../db.server";
import { authenticate } from "../shopify.server";

const DAYS = 30;

export const loader = async ({ request, params }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;
  const productId  = params.productId;

  const product = await db.shopifyProduct.findFirst({
    where: { id: productId, shopDomain },
    include: { variants: { select: { id: true, title: true, currentPrice: true, basePrice: true } } },
  });
  if (!product) throw new Response("Product not found", { status: 404 });

  const decisions = await db.priceDecision.findMany({
    where: {
      shopDomain,
      shopifyVariantId: { in: product.variants.map((v) => v.id) },
    },
    orderBy: { decidedAt: "desc" },
    take: 100,
  });

  const settings = await db.shopSettings.findUnique({ where: { shopDomain } });
  const minCompetitorsToPrice = settings?.minCompetitorsToPrice ?? 4;
  const strongMatchCount = await db.productLevelMatch.count({
    where: {
      shopifyProductId: productId, shopDomain,
      rejectedByMerchant: false,
      confidenceTier: { in: ["CONFIRMED", "LIKELY"] },
    },
  });

  // Find this product's competitor variants via ProductLevelMatch →
  // ScrapedProduct → ScrapedVariant, then pull 30d of observations.
  const matches = await db.productLevelMatch.findMany({
    where: { shopifyProductId: productId, shopDomain, rejectedByMerchant: false },
    orderBy: { confidence: "desc" },
    take: 8,
    include: {
      scrapedProduct: {
        select: { title: true, domain: true, variants: { select: { id: true } } },
      },
    },
  });

  const competitorVariantIds = matches.flatMap((m) => m.scrapedProduct.variants.map((v) => v.id));
  const since = new Date(Date.now() - DAYS * 24 * 3600 * 1000);
  const observations = competitorVariantIds.length === 0
    ? []
    : await db.competitorPriceObservation.findMany({
        where: { competitorVariantId: { in: competitorVariantIds }, observedAt: { gte: since } },
        orderBy: { observedAt: "asc" },
        select: { competitorVariantId: true, price: true, observedAt: true },
      });

  // Bundle observations per competitor product (collapse variants).
  const variantToProduct = new Map();
  for (const m of matches) {
    for (const v of m.scrapedProduct.variants) {
      variantToProduct.set(v.id, {
        id: m.scrapedProductId,
        title: m.scrapedProduct.title,
        domain: m.scrapedProduct.domain,
        confidence: Number(m.confidence),
      });
    }
  }
  const seriesByCompetitor = new Map();
  for (const o of observations) {
    const p = variantToProduct.get(o.competitorVariantId);
    if (!p) continue;
    if (!seriesByCompetitor.has(p.id)) {
      seriesByCompetitor.set(p.id, { ...p, points: [] });
    }
    seriesByCompetitor.get(p.id).points.push({
      t: o.observedAt.getTime(),
      price: Number(o.price),
    });
  }

  const competitorSeries = [...seriesByCompetitor.values()];

  return {
    waiting: {
      have: strongMatchCount,
      need: minCompetitorsToPrice,
    },
    product: {
      id: product.id,
      title: product.title,
      dynamicPricingEnabled: product.dynamicPricingEnabled,
      tier: product.pricingTier,
      basePrice: product.basePrice?.toString() ?? null,
      variants: product.variants.map((v) => ({
        id: v.id, title: v.title,
        currentPrice: Number(v.currentPrice),
        basePrice: v.basePrice ? Number(v.basePrice) : null,
      })),
    },
    decisions: decisions.map((d) => ({
      id: d.id,
      variantId: d.shopifyVariantId,
      oldPrice: Number(d.oldPrice),
      newPrice: Number(d.newPrice),
      changePct: d.changePct,
      refPrice: d.refPrice ? Number(d.refPrice) : null,
      tier: d.tierAtDecision,
      competitorsUsed: d.competitorsUsed,
      oosObservations: d.oosObservations,
      currencyDrops:   d.currencyDrops,
      skipReason: d.skipReason,
      autoApplied: d.autoApplied,
      appliedAt: d.appliedAt?.toISOString() ?? null,
      decidedAt: d.decidedAt.toISOString(),
      reason: d.reason,
      applyError: d.applyError,
    })),
    competitorSeries,
  };
};

// ─── SVG line chart ───────────────────────────────────────────────────────
function PriceChart({ competitorSeries, productPrice }) {
  const W = 720;
  const H = 240;
  const PAD = { l: 48, r: 12, t: 12, b: 28 };
  const innerW = W - PAD.l - PAD.r;
  const innerH = H - PAD.t - PAD.b;

  const allPoints = competitorSeries.flatMap((s) => s.points);
  if (allPoints.length === 0) {
    return (
      <s-text tone="subdued">
        No competitor observations in the last {DAYS} days yet.
      </s-text>
    );
  }

  const ts = allPoints.map((p) => p.t);
  const tMin = Math.min(...ts);
  const tMax = Math.max(...ts);
  const prices = allPoints.map((p) => p.price).concat([productPrice].filter(Boolean));
  const pMin = Math.min(...prices) * 0.95;
  const pMax = Math.max(...prices) * 1.05;

  const xy = (t, p) => [
    PAD.l + (innerW * (t - tMin)) / Math.max(1, tMax - tMin),
    PAD.t + innerH - (innerH * (p - pMin)) / Math.max(1, pMax - pMin),
  ];

  const palette = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ca8a04", "#0891b2", "#db2777", "#65a30d"];

  return (
    <svg width={W} height={H} style={{ background: "#fff", border: "1px solid #e5e7eb", borderRadius: 4 }}>
      {/* y-axis ticks */}
      {[0, 0.25, 0.5, 0.75, 1].map((f) => {
        const yVal = pMin + (pMax - pMin) * f;
        const y = PAD.t + innerH - innerH * f;
        return (
          <g key={f}>
            <line x1={PAD.l} y1={y} x2={W - PAD.r} y2={y} stroke="#f3f4f6" />
            <text x={PAD.l - 6} y={y + 4} fontSize="10" textAnchor="end" fill="#6b7280">
              ₹{yVal.toFixed(0)}
            </text>
          </g>
        );
      })}

      {/* Merchant current price as horizontal reference */}
      {productPrice && (() => {
        const [, y] = xy(tMin, productPrice);
        return (
          <line x1={PAD.l} y1={y} x2={W - PAD.r} y2={y}
                stroke="#111827" strokeDasharray="4 4" strokeWidth="1.5" />
        );
      })()}

      {/* Competitor lines */}
      {competitorSeries.map((s, i) => {
        const pts = s.points.map((p) => xy(p.t, p.price).join(",")).join(" ");
        return (
          <polyline key={s.id} points={pts} fill="none"
                    stroke={palette[i % palette.length]} strokeWidth="1.5" />
        );
      })}
    </svg>
  );
}

export default function ProductStatsPage() {
  const { product, decisions, competitorSeries, waiting } = useLoaderData();
  const primaryVariantPrice = product.variants[0]?.currentPrice;
  const needsMore = product.dynamicPricingEnabled && waiting.have < waiting.need;

  return (
    <s-page heading={`Stats: ${product.title}`}>
      <s-stack direction="block" gap="loose">
        <s-section>
          <s-stack direction="inline" gap="base" align="center">
            <s-badge tone="info">Tier: {product.tier}</s-badge>
            {product.basePrice && (
              <s-text tone="subdued">
                Base price anchor: ₹{Number(product.basePrice).toFixed(2)}
              </s-text>
            )}
            <Link to="/app">← Back to products</Link>
          </s-stack>
        </s-section>

        {needsMore && (
          <s-section>
            <s-banner tone="warning">
              <s-text emphasis="bold">
                Waiting for {waiting.need - waiting.have} more competitor
                {waiting.need - waiting.have === 1 ? "" : "s"}
              </s-text>
              <s-text tone="subdued">
                {" "}You have {waiting.have} CONFIRMED/LIKELY matches and need {waiting.need}
                before any price decision will run.{" "}
                <Link to={`/app/discover/${encodeURIComponent(product.id)}`}>
                  Find more competitors →
                </Link>
              </s-text>
            </s-banner>
          </s-section>
        )}

        <s-section heading="Competitor prices (last 30 days)">
          <s-stack direction="block" gap="tight">
            <PriceChart
              competitorSeries={competitorSeries}
              productPrice={primaryVariantPrice}
            />
            <s-stack direction="block" gap="tight">
              <s-text emphasis="bold">Legend</s-text>
              <s-text tone="subdued">
                Dashed black = your current price.
              </s-text>
              {competitorSeries.map((s) => (
                <s-text key={s.id} tone="subdued">
                  ● {s.domain} — {s.title} (similarity {(s.confidence * 100).toFixed(0)}%)
                </s-text>
              ))}
            </s-stack>
          </s-stack>
        </s-section>

        <s-section heading={`Decision history (${decisions.length})`}>
          {decisions.length === 0 ? (
            <s-text tone="subdued">No price decisions yet for this product.</s-text>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: "left", borderBottom: "1px solid #e5e7eb" }}>
                  <th style={{ padding: 6 }}>When</th>
                  <th style={{ padding: 6 }}>Old → New</th>
                  <th style={{ padding: 6 }}>Δ</th>
                  <th style={{ padding: 6 }}>Ref</th>
                  <th style={{ padding: 6 }}>Comps</th>
                  <th style={{ padding: 6 }}>Tier</th>
                  <th style={{ padding: 6 }}>Status</th>
                  <th style={{ padding: 6 }}>Reason</th>
                </tr>
              </thead>
              <tbody>
                {decisions.map((d) => (
                  <tr key={d.id} style={{ borderBottom: "1px solid #f3f4f6" }}>
                    <td style={{ padding: 6 }}>{new Date(d.decidedAt).toLocaleString()}</td>
                    <td style={{ padding: 6 }}>
                      ₹{d.oldPrice.toFixed(2)} → ₹{d.newPrice.toFixed(2)}
                    </td>
                    <td style={{ padding: 6, color: (d.changePct ?? 0) < 0 ? "#dc2626" : "#16a34a" }}>
                      {d.changePct != null ? `${(d.changePct * 100).toFixed(2)}%` : "—"}
                    </td>
                    <td style={{ padding: 6 }}>{d.refPrice != null ? `₹${d.refPrice.toFixed(2)}` : "—"}</td>
                    <td style={{ padding: 6 }}>
                      {d.competitorsUsed}
                      {(d.oosObservations > 0 || d.currencyDrops > 0) && (
                        <s-text tone="subdued">
                          {" ("}
                          {d.oosObservations > 0 && `${d.oosObservations} OOS`}
                          {d.oosObservations > 0 && d.currencyDrops > 0 && ", "}
                          {d.currencyDrops > 0 && `${d.currencyDrops} currency`}
                          {")"}
                        </s-text>
                      )}
                    </td>
                    <td style={{ padding: 6 }}>{d.tier ?? "—"}</td>
                    <td style={{ padding: 6 }}>
                      {d.appliedAt
                        ? <s-badge tone="success">Applied</s-badge>
                        : d.skipReason
                          ? <s-badge tone="subdued">{d.skipReason}</s-badge>
                          : <s-badge tone="warning">Pending</s-badge>}
                      {d.applyError && (
                        <s-text tone="critical"> {d.applyError}</s-text>
                      )}
                    </td>
                    <td style={{ padding: 6, color: "#6b7280" }}>{d.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </s-section>
      </s-stack>
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
