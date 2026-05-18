/* eslint-disable react/prop-types */
import { useState } from "react";
import {
  useFetcher, useLoaderData, useRouteError, Link, useNavigation,
} from "react-router";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ReferenceDot, ResponsiveContainer,
} from "recharts";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

// Lookback window for chart + logs. 30 days is plenty for the demo and keeps
// the merged time series small in the browser.
const LOOKBACK_DAYS = 30;

// Distinct colors for competitor lines. Cycled if there are more competitors
// than colors; my-price stays bold black.
const COMPETITOR_COLORS = [
  "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
  "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
];

export const loader = async ({ request, params }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const variantId = decodeURIComponent(params.variantId);

  const since = new Date(Date.now() - LOOKBACK_DAYS * 86_400_000);

  // Variant + product + product's CONFIRMED match
  const variant = await db.shopifyVariant.findFirst({
    where: { id: variantId, product: { shopDomain: shop } },
    select: {
      id: true, title: true, currentPrice: true, autoPriceEnabled: true,
      inventoryQuantity: true, cost: true,
      product: {
        select: {
          id: true, title: true, vendor: true,
          productLevelMatches: {
            where: { confidenceTier: "CONFIRMED" },
            select: { id: true, scrapedProductId: true },
          },
        },
      },
      competitorStats: {
        select: { competitorCount: true, weightedMin: true,
                  weightedMedian: true, minPrice: true, maxPrice: true,
                  lastUpdatedAt: true },
      },
    },
  });

  if (!variant) {
    throw new Response("Variant not found", { status: 404 });
  }

  // Decisions for this variant in window (most recent first for the log;
  // we re-sort for the chart later).
  const decisions = await db.priceDecision.findMany({
    where: { shopifyVariantId: variantId, decidedAt: { gte: since } },
    orderBy: { decidedAt: "desc" },
    take: 100,
    select: {
      id: true, decidedAt: true, appliedAt: true, revertedAt: true,
      oldPrice: true, newPrice: true,
      ruleSuggestedPrice: true, mlSuggestedPrice: true, mlConfidence: true,
      modelVersion: true,
      reason: true, blockedBy: true, confidence: true,
      ruleId: true,
    },
  });

  // PriceChange was folded into PriceDecision — an "applied change" is a
  // decision row with appliedAt != NULL. Keep the local `changes` shape so
  // downstream chart / revert code is unchanged.
  const changes = decisions
    .filter((d) => d.appliedAt)
    .map((d) => ({
      id:         d.id,
      appliedAt:  d.appliedAt,
      oldPrice:   d.oldPrice,
      newPrice:   d.newPrice,
      revertedAt: d.revertedAt,
      decisionId: d.id,
    }));

  // Competitor price lines: every ProductMatch the variant has → all
  // observations on that competitor variant in the window.
  const matches = await db.productMatch.findMany({
    where: { shopifyVariantId: variantId, competitorVariantId: { not: null } },
    select: {
      competitorVariantId: true,
      competitorVariant: {
        select: {
          id: true, title: true,
          product: { select: { id: true, title: true, domain: true } },
        },
      },
    },
  });

  const competitorVariantIds = matches
    .map((m) => m.competitorVariantId).filter(Boolean);

  const observations = competitorVariantIds.length > 0
    ? await db.competitorPriceObservation.findMany({
        where: {
          competitorVariantId: { in: competitorVariantIds },
          observedAt: { gte: since },
        },
        orderBy: { observedAt: "asc" },
        select: { competitorVariantId: true, price: true,
                  isInStock: true, observedAt: true },
      })
    : [];

  // Group observations per competitor variant; per domain pick the BEST
  // (lowest active) competitor variant by default to keep the chart readable.
  // We include all of them in the legend so the merchant can see what's tracked.
  const obsByVariant = {};
  observations.forEach((o) => {
    if (!obsByVariant[o.competitorVariantId]) obsByVariant[o.competitorVariantId] = [];
    obsByVariant[o.competitorVariantId].push({
      t: new Date(o.observedAt).getTime(),
      v: Number(o.price),
    });
  });

  const competitorSeries = matches
    .filter((m) => obsByVariant[m.competitorVariantId]?.length)
    .map((m, i) => ({
      key:    `c${i}`,
      label:  `${m.competitorVariant.product.domain} — ${m.competitorVariant.title}`,
      color:  COMPETITOR_COLORS[i % COMPETITOR_COLORS.length],
      points: obsByVariant[m.competitorVariantId],
    }));

  // My-price step series: reconstruct backwards from currentPrice using
  // PriceChange events. At each appliedAt, price moved old→new.
  const myPricePoints = [];
  const changesAsc = [...changes].sort(
    (a, b) => new Date(a.appliedAt) - new Date(b.appliedAt),
  );
  if (changesAsc.length === 0) {
    myPricePoints.push({ t: since.getTime(), v: Number(variant.currentPrice) });
    myPricePoints.push({ t: Date.now(),      v: Number(variant.currentPrice) });
  } else {
    const firstOld = Number(changesAsc[0].oldPrice);
    myPricePoints.push({ t: since.getTime(), v: firstOld });
    for (const ch of changesAsc) {
      myPricePoints.push({ t: new Date(ch.appliedAt).getTime() - 1, v: Number(ch.oldPrice) });
      myPricePoints.push({ t: new Date(ch.appliedAt).getTime(),     v: Number(ch.newPrice) });
    }
    myPricePoints.push({ t: Date.now(), v: Number(variant.currentPrice) });
  }

  return {
    shop,
    variant: {
      id:                variant.id,
      title:             variant.title,
      productTitle:      variant.product.title,
      productVendor:     variant.product.vendor,
      currentPrice:      Number(variant.currentPrice),
      cost:              variant.cost != null ? Number(variant.cost) : null,
      autoPriceEnabled:  variant.autoPriceEnabled,
      inventoryQuantity: variant.inventoryQuantity,
      hasConfirmedMatch: variant.product.productLevelMatches.length > 0,
      stats: variant.competitorStats ? {
        competitorCount: variant.competitorStats.competitorCount,
        weightedMin:     variant.competitorStats.weightedMin != null
          ? Number(variant.competitorStats.weightedMin) : null,
        weightedMedian:  variant.competitorStats.weightedMedian != null
          ? Number(variant.competitorStats.weightedMedian) : null,
        lastUpdatedAt:   variant.competitorStats.lastUpdatedAt,
      } : null,
    },
    decisions: decisions.map((d) => ({
      ...d,
      oldPrice:           Number(d.oldPrice),
      newPrice:           Number(d.newPrice),
      ruleSuggestedPrice: d.ruleSuggestedPrice != null
        ? Number(d.ruleSuggestedPrice) : null,
      confidence:         Number(d.confidence),
    })),
    changes: changes.map((c) => ({
      ...c,
      oldPrice: Number(c.oldPrice),
      newPrice: Number(c.newPrice),
    })),
    chart: {
      myPrice:     myPricePoints,
      competitors: competitorSeries,
      // Decision and change markers — rendered as ReferenceDots.
      changeMarkers: changesAsc.map((c) => ({
        t: new Date(c.appliedAt).getTime(),
        old: Number(c.oldPrice),
        new: Number(c.newPrice),
        reverted: !!c.revertedAt,
      })),
    },
  };
};

export const action = async ({ request, params }) => {
  const { admin, session } = await authenticate.admin(request);
  const shop = session.shop;
  const variantId = decodeURIComponent(params.variantId);
  const fd = await request.formData();
  const intent = fd.get("intent");

  // Ownership check shared by all intents.
  const variant = await db.shopifyVariant.findFirst({
    where: { id: variantId, product: { shopDomain: shop } },
    select: { id: true, productId: true, currentPrice: true },
  });
  if (!variant) return { ok: false, error: "not_found" };

  if (intent === "togglePause") {
    const enabled = fd.get("enabled") === "true";
    await db.shopifyVariant.update({
      where: { id: variantId },
      data:  { autoPriceEnabled: enabled },
    });
    return { ok: true, intent };
  }

  if (intent === "revert") {
    // changeId is now a PriceDecision id (PriceChange was folded in).
    const changeId = String(fd.get("changeId"));
    const change = await db.priceDecision.findFirst({
      where: { id: changeId, shopifyVariantId: variantId, appliedAt: { not: null } },
      select: { id: true, oldPrice: true, newPrice: true },
    });
    if (!change) return { ok: false, error: "change_not_found" };

    const targetPrice = Number(change.oldPrice);

    // Push to Shopify via Admin API. We're authenticated as the merchant
    // session, so no offline-token plumbing needed for this path.
    const resp = await admin.graphql(
      `#graphql
      mutation RevertPrice($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
        productVariantsBulkUpdate(productId: $productId, variants: $variants) {
          productVariants { id price }
          userErrors { field message }
        }
      }`,
      {
        variables: {
          productId: variant.productId,
          variants: [{ id: variantId, price: String(targetPrice.toFixed(2)) }],
        },
      },
    );
    const data = await resp.json();
    const userErrors = data?.data?.productVariantsBulkUpdate?.userErrors ?? [];
    if (userErrors.length) {
      return { ok: false, error: userErrors.map((e) => e.message).join("; ") };
    }

    // DB updates: stamp revertedAt on the source decision, drop the variant
    // to the target price locally, pause auto-pricing so the rule engine
    // doesn't immediately re-suggest the same change.
    await db.priceDecision.update({
      where: { id: changeId },
      data:  { revertedAt: new Date() },
    });
    await db.shopifyVariant.update({
      where: { id: variantId },
      data:  { currentPrice: targetPrice, autoPriceEnabled: false },
    });
    // Audit row: pseudo-decision so the log shows the revert event.
    await db.priceDecision.create({
      data: {
        shopDomain:       shop,
        shopifyVariantId: variantId,
        ruleId:           null,
        oldPrice:         Number(variant.currentPrice),
        newPrice:         targetPrice,
        reason:           `manual_revert of change ${changeId}`,
        confidence:       0,
        statsSnapshot:    {},
        signalsSnapshot:  { manualRevert: true, sourceChangeId: changeId },
        appliedAt:        new Date(),
      },
    });
    return { ok: true, intent };
  }

  return { ok: false, error: "unknown_intent" };
};

export default function VariantDeepDive() {
  const { variant, decisions, changes, chart } = useLoaderData();
  const nav = useNavigation();
  const busy = nav.state !== "idle";

  return (
    <s-page heading={`${variant.productTitle} — ${variant.title}`}>
      <s-section>
        <s-stack direction="inline" gap="base" alignment="space-between">
          <s-stack gap="tight">
            <s-text emphasis="bold">Current ₹{variant.currentPrice.toFixed(2)}</s-text>
            {variant.cost != null ? (
              <s-text tone="subdued">cost: ₹{variant.cost.toFixed(2)}</s-text>
            ) : null}
            {variant.inventoryQuantity != null ? (
              <s-text tone="subdued">stock: {variant.inventoryQuantity}</s-text>
            ) : null}
            <s-text tone={variant.hasConfirmedMatch ? "success" : "critical"}>
              {variant.hasConfirmedMatch
                ? "Has CONFIRMED competitor match — eligible for auto-pricing"
                : "No CONFIRMED match yet — pricing is blocked"}
            </s-text>
          </s-stack>
          <Link to="/app/pricing"><s-button variant="secondary">← Back</s-button></Link>
        </s-stack>
      </s-section>

      <s-section heading="Price history (last 30 days)">
        {chart.myPrice.length === 0 && chart.competitors.length === 0 ? (
          <s-text tone="subdued">No price data yet. Scrape competitors and wait for the next pricing decision.</s-text>
        ) : (
          <PriceChart chart={chart} />
        )}
        <s-text tone="subdued">
          ● = price-change event. Hover a dot for details. Use the buttons below
          to revert a specific change or pause auto-pricing.
        </s-text>
      </s-section>

      <s-section heading="Quick actions">
        <QuickActions variant={variant} busy={busy} />
      </s-section>

      <s-section heading="Applied changes">
        <ChangeLog changes={changes} variantId={variant.id} busy={busy} />
      </s-section>

      <s-section heading="Recent decisions">
        <DecisionLog decisions={decisions} />
      </s-section>
    </s-page>
  );
}

function PriceChart({ chart }) {
  // Build a single merged dataset: for each unique timestamp, fill the value
  // for whatever series has a sample there. Recharts handles null gracefully
  // by leaving gaps; with connectNulls we get continuous lines.
  const points = new Map();

  function add(t, key, v) {
    if (!points.has(t)) points.set(t, { t });
    points.get(t)[key] = v;
  }
  chart.myPrice.forEach((p) => add(p.t, "my", p.v));
  chart.competitors.forEach((cs) =>
    cs.points.forEach((p) => add(p.t, cs.key, p.v))
  );
  const data = [...points.values()].sort((a, b) => a.t - b.t);

  return (
    <div style={{ width: "100%", height: 360 }}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 16, right: 24, bottom: 8, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e4e5e7" />
          <XAxis
            dataKey="t"
            type="number"
            domain={["dataMin", "dataMax"]}
            tickFormatter={(t) => new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" })}
            stroke="#6d7175"
          />
          <YAxis
            stroke="#6d7175"
            tickFormatter={(v) => `₹${v}`}
            domain={["auto", "auto"]}
          />
          <Tooltip
            labelFormatter={(t) => new Date(t).toLocaleString()}
            formatter={(v, name) => [`₹${Number(v).toFixed(2)}`, name]}
          />
          <Legend />
          <Line
            type="stepAfter"
            dataKey="my"
            name="My price"
            stroke="#0a0a0a"
            strokeWidth={3}
            dot={false}
            connectNulls
            isAnimationActive={false}
          />
          {chart.competitors.map((c) => (
            <Line
              key={c.key}
              type="monotone"
              dataKey={c.key}
              name={c.label}
              stroke={c.color}
              strokeWidth={1.5}
              dot={{ r: 2 }}
              connectNulls
              isAnimationActive={false}
            />
          ))}
          {chart.changeMarkers.map((m, i) => (
            <ReferenceDot
              key={i}
              x={m.t}
              y={m.new}
              r={6}
              fill={m.reverted ? "#9aa0a6" : "#bf0711"}
              stroke="#fff"
              strokeWidth={2}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function QuickActions({ variant, busy }) {
  const fetcher = useFetcher();
  return (
    <s-stack direction="inline" gap="base">
      <fetcher.Form method="post">
        <input type="hidden" name="intent" value="togglePause" />
        <input type="hidden" name="enabled" value={String(!variant.autoPriceEnabled)} />
        <s-button
          type="submit"
          variant={variant.autoPriceEnabled ? "secondary" : "primary"}
          disabled={busy || (!variant.autoPriceEnabled && !variant.hasConfirmedMatch)}
        >
          {variant.autoPriceEnabled
            ? "⏸ Pause auto-pricing"
            : "▶ Resume auto-pricing"}
        </s-button>
      </fetcher.Form>
    </s-stack>
  );
}

function ChangeLog({ changes, variantId, busy }) {
  const fetcher = useFetcher();
  if (changes.length === 0) {
    return <s-text tone="subdued">No applied changes yet.</s-text>;
  }
  return (
    <s-stack gap="tight">
      {changes.map((c) => {
        const delta = c.newPrice - c.oldPrice;
        const arrow = delta > 0 ? "↑" : delta < 0 ? "↓" : "·";
        const tone = c.revertedAt ? "subdued" : (delta < 0 ? "success" : "critical");
        return (
          <s-stack
            key={c.id}
            direction="inline"
            gap="base"
            alignment="space-between"
            style={{
              padding: 8,
              borderRadius: 6,
              background: c.revertedAt ? "#f6f6f7" : "transparent",
              border: "1px solid #e4e5e7",
            }}
          >
            <s-stack gap="tight">
              <s-text>
                <span style={{ color: tone === "success" ? "#0a5a2a" : tone === "critical" ? "#bf0711" : "#6d7175" }}>
                  {arrow}
                </span>
                {" "}₹{c.oldPrice.toFixed(2)} → ₹{c.newPrice.toFixed(2)}
                {c.revertedAt ? "  (reverted)" : ""}
              </s-text>
              <s-text tone="subdued">{new Date(c.appliedAt).toLocaleString()}</s-text>
            </s-stack>
            {!c.revertedAt && (
              <ConfirmRevert id={c.id} oldPrice={c.oldPrice} fetcher={fetcher} busy={busy} variantId={variantId} />
            )}
          </s-stack>
        );
      })}
    </s-stack>
  );
}

function ConfirmRevert({ id, oldPrice, fetcher, busy }) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <s-button variant="secondary" onClick={() => setConfirming(true)} disabled={busy}>
        ↶ Revert to ₹{oldPrice.toFixed(2)}
      </s-button>
    );
  }
  return (
    <fetcher.Form method="post">
      <input type="hidden" name="intent" value="revert" />
      <input type="hidden" name="changeId" value={id} />
      <s-stack direction="inline" gap="tight">
        <s-button variant="secondary" onClick={() => setConfirming(false)} disabled={busy}>
          Cancel
        </s-button>
        <s-button type="submit" tone="critical" variant="primary" disabled={busy}>
          Confirm revert
        </s-button>
      </s-stack>
    </fetcher.Form>
  );
}

function DecisionLog({ decisions }) {
  if (decisions.length === 0) {
    return <s-text tone="subdued">No decisions logged yet.</s-text>;
  }
  return (
    <s-stack gap="tight">
      {decisions.map((d) => {
        const blocked = d.blockedBy != null;
        return (
          <s-stack key={d.id} gap="tight"
            style={{
              padding: 8, borderRadius: 6,
              border: "1px solid #e4e5e7",
              background: blocked ? "#fff5f5" : "transparent",
            }}>
            <s-stack direction="inline" gap="base" alignment="space-between">
              <s-text>
                ₹{d.oldPrice.toFixed(2)} → ₹{d.newPrice.toFixed(2)}
                {" · "}conf {(d.confidence * 100).toFixed(0)}%
                {blocked ? <span style={{ color: "#bf0711" }}> · blocked: {d.blockedBy}</span> : null}
              </s-text>
              <s-text tone="subdued">{new Date(d.decidedAt).toLocaleString()}</s-text>
            </s-stack>
            <s-text tone="subdued">{d.reason}</s-text>
            {d.ruleSuggestedPrice != null && d.ruleSuggestedPrice !== d.newPrice ? (
              <s-text tone="subdued">
                rule suggested ₹{d.ruleSuggestedPrice.toFixed(2)} · final ₹{d.newPrice.toFixed(2)}
              </s-text>
            ) : null}
          </s-stack>
        );
      })}
    </s-stack>
  );
}

export function ErrorBoundary() {
  return boundary.error(useRouteError());
}
