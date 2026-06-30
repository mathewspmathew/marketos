/**
 * app.stats.$productId.jsx
 *
 * Per-product auto-pricing stats:
 *   - Header chips (tier / base / current) + open-in-Shopify-admin link
 *   - Empty-state banner when matches < minCompetitorsToPrice
 *   - 30-day competitor price chart (inline SVG, no chart lib)
 *   - Decision history table with explicit applied / failed / pending /
 *     skipped status — distinct from "intent to apply"
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
    include: { ShopifyVariant: { select: { id: true, title: true, currentPrice: true, basePrice: true } } },
  });
  if (!product) throw new Response("Product not found", { status: 404 });

  const decisions = await db.priceDecision.findMany({
    where: {
      shopDomain,
      shopifyVariantId: { in: product.ShopifyVariant.map((v) => v.id) },
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

  const matches = await db.productLevelMatch.findMany({
    where: { shopifyProductId: productId, shopDomain, rejectedByMerchant: false },
    orderBy: { confidence: "desc" },
    take: 8,
    include: {
      ScrapedProduct: {
        select: { title: true, domain: true, ScrapedVariant: { select: { id: true } } },
      },
    },
  });

  const competitorVariantIds = matches.flatMap((m) => m.ScrapedProduct.ScrapedVariant.map((v) => v.id));
  const since = new Date(Date.now() - DAYS * 24 * 3600 * 1000);
  const observations = competitorVariantIds.length === 0
    ? []
    : await db.competitorPriceObservation.findMany({
        where: { competitorVariantId: { in: competitorVariantIds }, observedAt: { gte: since } },
        orderBy: { observedAt: "asc" },
        select: { competitorVariantId: true, price: true, observedAt: true },
      });

  const variantToProduct = new Map();
  for (const m of matches) {
    for (const v of m.ScrapedProduct.ScrapedVariant) {
      variantToProduct.set(v.id, {
        id: m.scrapedProductId,
        title: m.ScrapedProduct.title,
        domain: m.ScrapedProduct.domain,
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

  // Shopify admin URL for the "open in Shopify" link. Numeric id parsed
  // from the GraphQL gid; store handle parsed from the shop domain.
  const numericProductId = product.id.split("/").pop();
  const storeHandle = shopDomain.replace(".myshopify.com", "");
  const adminProductUrl = `https://admin.shopify.com/store/${storeHandle}/products/${numericProductId}`;

  return {
    waiting: { have: strongMatchCount, need: minCompetitorsToPrice },
    product: {
      id: product.id,
      title: product.title,
      dynamicPricingEnabled: product.dynamicPricingEnabled,
      tier: product.pricingTier,
      basePrice: product.basePrice ? Number(product.basePrice) : null,
      adminProductUrl,
      variants: product.ShopifyVariant.map((v) => ({
        id: v.id, title: v.title,
        currentPrice: Number(v.currentPrice),
        basePrice: v.basePrice ? Number(v.basePrice) : null,
      })),
    },
    decisions: decisions.map((d) => {
      // Distinguish four lifecycle states explicitly so the UI can't be
      // misleading (an autoApplied=true row with appliedAt=NULL means Shopify
      // rejected the push — NOT that we applied it).
      const isApplied = !!d.appliedAt;
      const status = isApplied
        ? "applied"
        : d.applyError
          ? "failed"
          : d.autoApplied
            ? "pending"      // intent set, Shopify push hasn't succeeded yet
            : "skipped";     // decide step didn't even try (skipReason explains why)
      const clampReason = d.skipReason && d.skipReason.startsWith("clamped_") ? d.skipReason : null;
      const skipReasonNotClamp = d.skipReason && !d.skipReason.startsWith("clamped_") ? d.skipReason : null;
      return {
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
        appliedAt: d.appliedAt?.toISOString() ?? null,
        decidedAt: d.decidedAt.toISOString(),
        reason: d.reason,
        applyError: d.applyError,
        status,
        clampReason,
        skipReason: skipReasonNotClamp,
      };
    }),
    competitorSeries: [...seriesByCompetitor.values()],
  };
};

/**
 * Parse pricing reason and clamp type to generate merchant-friendly explanation.
 * Returns {line1, line2} or null if reason format doesn't match or clampReason is unknown.
 */
function getClampExplanation(clampReason, decision) {
  if (!clampReason) return null;

  // Parse reason string: "ref=X target=Y tier=Z comps=N"
  const reasonMatch = decision.reason.match(
    /ref=([\d.]+)\s+target=([\d.]+)\s+tier=(\w+)\s+comps=(\d+)/
  );
  if (!reasonMatch) return null;

  const [, ref, target, tier, comps] = reasonMatch;
  const targetPrice = parseFloat(target);
  const appliedPrice = decision.newPrice;

  if (clampReason === 'clamped_per_round') {
    return {
      line1: `Target ₹${targetPrice.toFixed(2)} → ₹${appliedPrice.toFixed(2)} (per-round cap)`,
      line2: `Limited by maximum change per cycle. Reference: ₹${ref} from ${comps} competitors (${tier} tier).`,
    };
  }

  if (clampReason === 'clamped_lifetime_cap') {
    return {
      line1: `Target ₹${targetPrice.toFixed(2)} → ₹${appliedPrice.toFixed(2)} (lifetime cap)`,
      line2: `Price adjusted to stay within allowed range. Reference: ₹${ref} from ${comps} competitors (${tier} tier).`,
    };
  }

  return null;
}

// ─── SVG line chart (Polaris has no built-in chart primitive) ─────────────
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
      {productPrice && (() => {
        const [, y] = xy(tMin, productPrice);
        return (
          <line x1={PAD.l} y1={y} x2={W - PAD.r} y2={y}
                stroke="#111827" strokeDasharray="4 4" strokeWidth="1.5" />
        );
      })()}
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

// ─── Status pill ──────────────────────────────────────────────────────────
function StatusBadge({ status }) {
  switch (status) {
    case "applied":
      return <s-badge tone="success">Applied to Shopify</s-badge>;
    case "failed":
      return <s-badge tone="critical">Push failed</s-badge>;
    case "pending":
      return <s-badge tone="warning">Pending push</s-badge>;
    case "skipped":
    default:
      return <s-badge tone="subdued">Skipped</s-badge>;
  }
}

export { getClampExplanation };

export default function ProductStatsPage() {
  const { product, decisions, competitorSeries, waiting } = useLoaderData();
  const primaryVariantPrice = product.variants[0]?.currentPrice;
  const needsMore = product.dynamicPricingEnabled && waiting.have < waiting.need;

  return (
    <s-page heading={product.title}>
      <s-section>
        <s-stack direction="inline" gap="base" align="center" wrap>
          <Link to="/app/stats">← Back to all stats</Link>
          <s-link href={product.adminProductUrl} target="_blank">
            Open in Shopify admin ↗
          </s-link>
          <s-badge tone="info">Tier: {product.tier}</s-badge>
          <s-text tone="subdued">
            Current ₹{primaryVariantPrice?.toFixed(2) ?? "—"}
            {product.basePrice && ` · Base ₹${Number(product.basePrice).toFixed(2)}`}
          </s-text>
        </s-stack>
      </s-section>

      {needsMore && (
        <s-section>
          <s-banner tone="warning">
            <s-text emphasis="bold">
              Waiting for {waiting.need - waiting.have} more competitor
              {waiting.need - waiting.have === 1 ? "" : "s"}.
            </s-text>
            <s-text tone="subdued">
              {" "}You have {waiting.have} CONFIRMED/LIKELY matches, need {waiting.need}.{" "}
              <Link to={`/app/discover/${encodeURIComponent(product.id)}`}>
                Find more competitors →
              </Link>
            </s-text>
          </s-banner>
        </s-section>
      )}

      <s-section heading="Competitor prices (last 30 days)">
        <s-stack direction="block" gap="base">
          <PriceChart competitorSeries={competitorSeries} productPrice={primaryVariantPrice} />
          <s-stack direction="block" gap="tight">
            <s-text emphasis="bold">Legend</s-text>
            <s-text tone="subdued">Dashed black line = your current price.</s-text>
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
              <tr style={{ textAlign: "left", borderBottom: "1px solid var(--p-color-border, #e5e7eb)" }}>
                <th style={{ padding: 8, fontWeight: 600 }}>When</th>
                <th style={{ padding: 8, fontWeight: 600 }}>Old → New</th>
                <th style={{ padding: 8, fontWeight: 600 }}>Δ</th>
                <th style={{ padding: 8, fontWeight: 600 }}>Ref</th>
                <th style={{ padding: 8, fontWeight: 600 }}>Comps</th>
                <th style={{ padding: 8, fontWeight: 600 }}>Tier</th>
                <th style={{ padding: 8, fontWeight: 600 }}>Status</th>
                <th style={{ padding: 8, fontWeight: 600 }}>Reason</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((d) => (
                <tr key={d.id} style={{ borderBottom: "1px solid #f3f4f6" }}>
                  <td style={{ padding: 8 }}>{new Date(d.decidedAt).toLocaleString()}</td>
                  <td style={{ padding: 8 }}>
                    ₹{d.oldPrice.toFixed(2)} → ₹{d.newPrice.toFixed(2)}
                  </td>
                  <td style={{ padding: 8, color: (d.changePct ?? 0) < 0 ? "#dc2626" : "#16a34a" }}>
                    {d.changePct != null ? `${(d.changePct * 100).toFixed(2)}%` : "—"}
                  </td>
                  <td style={{ padding: 8 }}>
                    {d.refPrice != null ? `₹${d.refPrice.toFixed(2)}` : "—"}
                  </td>
                  <td style={{ padding: 8 }}>
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
                  <td style={{ padding: 8 }}>{d.tier ?? "—"}</td>
                  <td style={{ padding: 8 }}>
                    <s-stack direction="block" gap="tight">
                      <StatusBadge status={d.status} />
                      {d.clampReason && (
                        (() => {
                          const explanation = getClampExplanation(d.clampReason, d);
                          if (!explanation) return d.clampReason;
                          return (
                            <s-stack direction="block" gap="tight">
                              <s-text tone="subdued">{explanation.line1}</s-text>
                              <s-text tone="subdued" size="small">{explanation.line2}</s-text>
                            </s-stack>
                          );
                        })()
                      )}
                      {d.skipReason && (
                        <s-text tone="subdued">{d.skipReason}</s-text>
                      )}
                      {d.applyError && (
                        <s-text tone="critical">{d.applyError}</s-text>
                      )}
                    </s-stack>
                  </td>
                  <td style={{ padding: 8, color: "#6b7280" }}>{d.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </s-section>
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
