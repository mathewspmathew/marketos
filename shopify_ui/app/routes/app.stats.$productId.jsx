/**
 * app.stats.$productId.jsx
 *
 * Per-product auto-pricing stats. Pure presentation layer — all data
 * (usable-competitor gate, decision lifecycle status, clamp explanations,
 * competitor price series) is computed by services/pricing_svc/product_stats.py
 * and fetched as-is, so the browser UI and any other client (e.g. the
 * chatbot) see identical numbers:
 *   - Header chips (tier / base / current) + open-in-Shopify-admin link
 *   - Empty-state banner when matches < minCompetitorsToPrice
 *   - 30-day competitor price chart (inline SVG, no chart lib — Polaris
 *     Viz was evaluated and rejected, see docs/superpowers/specs/
 *     2026-07-23-stats-matches-polaris-ui-refactor-design.md)
 *   - Decision history table (s-table) with explicit applied / failed /
 *     pending / skipped status — distinct from "intent to apply"
 */
import { useEffect } from "react";
import { useFetcher, useLoaderData, useRevalidator } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_HEADERS = { "X-Internal-Token": process.env.INTERNAL_API_TOKEN };
const DAYS = 30;

export const loader = async ({ request, params }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;
  const productId  = params.productId;

  const res = await fetch(
    `${PYTHON_API_URL}/internal/dynamic-pricing/product-stats?` +
    `shop_domain=${encodeURIComponent(shopDomain)}&product_id=${encodeURIComponent(productId)}`,
    { headers: INTERNAL_HEADERS },
  );
  const data = await res.json();
  if (!data.ok) throw new Response(data.error || "Product not found", { status: 404 });

  return { product: data.product, decisions: data.decisions, competitorSeries: data.competitorSeries, waiting: data.waiting };
};

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;
  const formData = await request.formData();
  const variantId = formData.get("variantId");
  const decisionId = formData.get("decisionId");

  const res = await fetch(`${PYTHON_API_URL}/internal/pricing/revert`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...INTERNAL_HEADERS },
    body: JSON.stringify({ shop_domain: shopDomain, variant_id: variantId, decision_id: decisionId }),
  });
  const data = await res.json();
  return data.ok ? { ok: true } : { ok: false, error: data.error };
};

// ─── SVG line chart (Polaris has no built-in chart primitive; polaris-viz is decommissioned) ─
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
    <s-box padding="base" borderRadius="base" borderWidth="base" borderColor="base">
      <svg width={W} height={H}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => {
          const yVal = pMin + (pMax - pMin) * f;
          const y = PAD.t + innerH - innerH * f;
          return (
            <g key={f}>
              <line x1={PAD.l} y1={y} x2={W - PAD.r} y2={y} stroke="var(--s-color-border, #f3f4f6)" />
              <text x={PAD.l - 6} y={y + 4} fontSize="10" textAnchor="end" fill="var(--s-color-text-subdued, #6b7280)">
                ₹{yVal.toFixed(0)}
              </text>
            </g>
          );
        })}
        {productPrice && (() => {
          const [, y] = xy(tMin, productPrice);
          return (
            <line x1={PAD.l} y1={y} x2={W - PAD.r} y2={y}
                  stroke="var(--s-color-text, #111827)" strokeDasharray="4 4" strokeWidth="1.5" />
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
    </s-box>
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

export default function ProductStatsPage() {
  const { product, decisions, competitorSeries, waiting } = useLoaderData();
  const primaryVariantPrice = product.variants[0]?.currentPrice;
  const needsMore = product.dynamicPricingEnabled && waiting.have < waiting.need;

  const revertFetcher = useFetcher();
  const revalidator = useRevalidator();
  const reverting = revertFetcher.state !== "idle";

  useEffect(() => {
    if (revertFetcher.state === "idle" && revertFetcher.data?.ok) {
      revalidator.revalidate();
    }
  }, [revertFetcher.state, revertFetcher.data, revalidator]);

  const revert = (variantId, decisionId) => {
    revertFetcher.submit({ variantId, decisionId }, { method: "POST" });
  };

  return (
    <s-page heading={product.title}>
      <s-section>
        <s-stack direction="inline" gap="base" alignItems="center" wrap>
          <s-button variant="plain" icon="arrow-left" href="/app/stats">Back to all stats</s-button>
          <s-link href={product.adminProductUrl} target="_blank">
            Open in Shopify admin ↗
          </s-link>
          <s-badge tone="info">Tier: {product.tier}</s-badge>
          <s-text tone="subdued">
            Current ₹{primaryVariantPrice?.toFixed(2) ?? "—"}
            {product.avgBasePrice && ` · Base ₹${Number(product.avgBasePrice).toFixed(2)}`}
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
              Not enough competitors? Pause this product on the Products page, adjust the
              competitor search settings, then Resume.
            </s-text>
          </s-banner>
        </s-section>
      )}

      {revertFetcher.data?.ok === false && (
        <s-section>
          <s-banner tone="critical">
            <s-text>{revertFetcher.data.error}</s-text>
          </s-banner>
        </s-section>
      )}

      {revertFetcher.data?.ok === true && (
        <s-section>
          <s-banner tone="success">
            <s-text>
              Price reverted and pushed to Shopify. Dynamic pricing has been paused for this
              product so it is not immediately re-applied — resume it from the Products page
              when ready.
            </s-text>
          </s-banner>
        </s-section>
      )}

      <s-section heading="Competitor prices (last 30 days)">
        <s-stack direction="block" gap="base">
          <PriceChart competitorSeries={competitorSeries} productPrice={primaryVariantPrice} />
          <s-stack direction="block" gap="small">
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
          <s-table>
            <s-table-header-row>
              <s-table-header listSlot="primary">When</s-table-header>
              <s-table-header listSlot="secondary">Old → New</s-table-header>
              <s-table-header listSlot="labeled" format="numeric">Δ</s-table-header>
              <s-table-header listSlot="labeled" format="currency">Ref</s-table-header>
              <s-table-header listSlot="labeled" format="numeric">Comps</s-table-header>
              <s-table-header listSlot="inline">Tier</s-table-header>
              <s-table-header listSlot="labeled">Status</s-table-header>
              <s-table-header listSlot="labeled">Reason</s-table-header>
              <s-table-header listSlot="inline">Actions</s-table-header>
            </s-table-header-row>
            <s-table-body>
              {decisions.map((d) => (
                <s-table-row key={d.id}>
                  <s-table-cell>{new Date(d.decidedAt).toLocaleString()}</s-table-cell>
                  <s-table-cell>
                    ₹{d.oldPrice.toFixed(2)} → ₹{d.newPrice.toFixed(2)}
                  </s-table-cell>
                  <s-table-cell>
                    <s-text tone={(d.changePct ?? 0) < 0 ? "critical" : "success"}>
                      {d.changePct != null ? `${(d.changePct * 100).toFixed(2)}%` : "—"}
                    </s-text>
                  </s-table-cell>
                  <s-table-cell>
                    {d.refPrice != null ? `₹${d.refPrice.toFixed(2)}` : "—"}
                  </s-table-cell>
                  <s-table-cell>
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
                  </s-table-cell>
                  <s-table-cell>{d.tier ?? "—"}</s-table-cell>
                  <s-table-cell>
                    <s-stack direction="block" gap="small">
                      <StatusBadge status={d.status} />
                      {d.revertedAt && <s-badge tone="subdued">Reverted</s-badge>}
                      {d.clampReason && (
                        d.clampExplanation ? (
                          <s-stack direction="block" gap="small">
                            <s-text tone="subdued">{d.clampExplanation.line1}</s-text>
                            <s-text tone="subdued" type="small">{d.clampExplanation.line2}</s-text>
                          </s-stack>
                        ) : d.clampReason
                      )}
                      {d.skipReason && (
                        <s-text tone="subdued">{d.skipReason}</s-text>
                      )}
                      {d.applyError && (
                        <s-text tone="critical">{d.applyError}</s-text>
                      )}
                    </s-stack>
                  </s-table-cell>
                  <s-table-cell>
                    <s-text tone="subdued">{d.reason}</s-text>
                  </s-table-cell>
                  <s-table-cell>
                    {d.status === "applied" && !d.revertedAt && (
                      <s-button
                        size="slim"
                        variant="plain"
                        tone="critical"
                        disabled={reverting}
                        onClick={() => revert(d.variantId, d.id)}
                      >
                        Revert
                      </s-button>
                    )}
                  </s-table-cell>
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
