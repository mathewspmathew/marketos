/* eslint-disable react/prop-types */
import { useState } from "react";
import { useFetcher, useLoaderData, useRouteError, useSearchParams, Link } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

const TABS = [
  { key: "active", label: "Active" },        // default — auto-pricing ON for ≥1 variant
  { key: "paused", label: "Paused" },        // confirmed match, but no active variants
];

// Product-level live view: one collapsed row per merchant product that has a
// CONFIRMED competitor match AND at least one variant with auto-pricing on.
// Default view is merchant-only (image, title, variants, current price, last
// pricing event). Click to expand for competitor list + per-variant price
// history + controls.
export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const url = new URL(request.url);
  const tab = url.searchParams.get("tab") || "active";

  const matches = await db.productLevelMatch.findMany({
    where: {
      shopDomain: shop,
      OR: [
        { confidenceTier: "CONFIRMED" },
        { reviewStatus: "CONFIRMED" },
      ],
    },
    orderBy: [{ confidence: "desc" }],
    include: {
      ShopifyProduct: {
        select: {
          id: true, title: true, vendor: true, imageUrl: true,
          dynamicPricingEnabled: true,
          ShopifyVariant: {
            select: {
              id: true, title: true, currentPrice: true,
              autoPriceEnabled: true, inventoryQuantity: true,
              // Last 5 applied decisions per variant for the history panel.
              priceDecisions: {
                where: { appliedAt: { not: null } },
                orderBy: { appliedAt: "desc" },
                take: 5,
                select: {
                  id: true, appliedAt: true, oldPrice: true, newPrice: true,
                  ruleSuggestedPrice: true, mlSuggestedPrice: true,
                  revertedAt: true,
                },
              },
            },
            orderBy: { currentPrice: "asc" },
          },
        },
      },
      ScrapedProduct: {
        select: {
          id: true, title: true, domain: true, imageUrl: true,
          ScrapedVariant: {
            select: { id: true, title: true, currentPrice: true,
                      isInStock: true },
            orderBy: { currentPrice: "asc" },
            take: 1,
          },
        },
      },
    },
  });

  // Same product can appear in multiple ProductLevelMatch rows (one per
  // competitor). Group by merchant product so the row is per-product, then
  // collect every confirmed competitor under it for the expanded panel.
  const byProduct = new Map();
  for (const m of matches) {
    const sp = m.ShopifyProduct;
    const cp = m.ScrapedProduct;
    // Don't filter out products with no active variants — they belong on the
    // "Paused" tab so the merchant can turn them back on without hunting.

    if (!byProduct.has(sp.id)) {
      const variants = sp.ShopifyVariant.map((v) => ({
        id:               v.id,
        title:            v.title,
        currentPrice:     Number(v.currentPrice),
        autoPriceEnabled: v.autoPriceEnabled,
        inventory:        v.inventoryQuantity,
        history: v.priceDecisions.map((d) => ({
          id:         d.id,
          appliedAt:  d.appliedAt,
          oldPrice:   Number(d.oldPrice),
          newPrice:   Number(d.newPrice),
          rulePrice:  d.ruleSuggestedPrice != null ? Number(d.ruleSuggestedPrice) : null,
          mlPrice:    d.mlSuggestedPrice   != null ? Number(d.mlSuggestedPrice)   : null,
          reverted:   !!d.revertedAt,
        })),
      }));
      const prices = variants.map((v) => v.currentPrice).filter((n) => n > 0);
      const minP = prices.length ? Math.min(...prices) : null;
      const maxP = prices.length ? Math.max(...prices) : null;
      const lastApplied = variants
        .flatMap((v) => v.history.map((h) => ({ ...h, variantId: v.id, variantTitle: v.title })))
        .sort((a, b) => new Date(b.appliedAt) - new Date(a.appliedAt))[0] || null;

      byProduct.set(sp.id, {
        productId:        sp.id,
        title:            sp.title,
        vendor:           sp.vendor,
        imageUrl:         sp.imageUrl,
        minPrice:         minP,
        maxPrice:         maxP,
        totalVariants:    variants.length,
        activeVariants:   variants.filter((v) => v.autoPriceEnabled).length,
        variants,
        lastApplied,
        competitors:      [],
      });
    }

    byProduct.get(sp.id).competitors.push({
      matchId:    m.id,
      confidence: m.confidence != null ? Number(m.confidence) : null,
      title:      cp.title,
      domain:     cp.domain,
      imageUrl:   cp.imageUrl,
      minPrice:   cp.ScrapedVariant[0]?.currentPrice != null
                    ? Number(cp.ScrapedVariant[0].currentPrice) : null,
      inStock:    cp.ScrapedVariant[0]?.isInStock ?? null,
    });
  }

  const allRows = Array.from(byProduct.values()).sort((a, b) =>
    (b.lastApplied?.appliedAt ? new Date(b.lastApplied.appliedAt) : 0) -
    (a.lastApplied?.appliedAt ? new Date(a.lastApplied.appliedAt) : 0)
  );

  const counts = {
    active: allRows.filter((r) => r.activeVariants > 0).length,
    paused: allRows.filter((r) => r.activeVariants === 0).length,
  };
  const rows = tab === "paused"
    ? allRows.filter((r) => r.activeVariants === 0)
    : allRows.filter((r) => r.activeVariants > 0);

  const pendingApprovals = await db.productLevelMatch.count({
    where: {
      shopDomain: shop,
      confidenceTier: "LIKELY",
      reviewedAt: null,
      reviewStatus: { not: "REJECTED" },
    },
  });

  return { shop, tab, rows, counts, pendingApprovals };
};

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const fd = await request.formData();
  const intent = fd.get("intent");

  if (intent === "toggleProductAutoPrice") {
    const productId = String(fd.get("productId") || "");
    const enabled   = fd.get("enabled") === "true";
    if (!productId) return { ok: false, error: "missing_productId" };
    // Ownership check via product.shopDomain.
    const owned = await db.shopifyProduct.findFirst({
      where: { id: productId, shopDomain: shop },
      select: { id: true },
    });
    if (!owned) return { ok: false, error: "not_found" };
    await db.shopifyVariant.updateMany({
      where: { productId },
      data:  { autoPriceEnabled: enabled },
    });
    return { ok: true, intent };
  }

  if (intent === "toggleVariantAutoPrice") {
    const variantId = String(fd.get("variantId") || "");
    const enabled   = fd.get("enabled") === "true";
    if (!variantId) return { ok: false, error: "missing_variantId" };
    await db.shopifyVariant.updateMany({
      where: { id: variantId, ShopifyProduct: { shopDomain: shop } },
      data:  { autoPriceEnabled: enabled },
    });
    return { ok: true, intent };
  }

  return { ok: false, error: "unknown_intent" };
};

export default function DynamicPricingPage() {
  const { rows, tab, counts, pendingApprovals } = useLoaderData();
  const [searchParams, setSearchParams] = useSearchParams();

  return (
    <s-page heading="Dynamic pricing">
      <s-section>
        <s-text>
          These products have a confirmed competitor match. Use the tabs below
          to switch between products currently being repriced and ones you
          have paused. Click any row to see competitors, history, and per-variant
          controls.
        </s-text>
      </s-section>

      <s-section>
        <s-stack direction="inline" gap="tight">
          {TABS.map((t) => (
            <s-button
              key={t.key}
              variant={tab === t.key ? "primary" : "secondary"}
              onClick={() => {
                const next = new URLSearchParams(searchParams);
                next.set("tab", t.key);
                setSearchParams(next);
              }}
            >
              {t.label} ({counts[t.key] ?? 0})
            </s-button>
          ))}
        </s-stack>
      </s-section>

      {rows.length === 0 ? (
        <EmptyState tab={tab} pendingApprovals={pendingApprovals}
                    pausedCount={counts.paused} activeCount={counts.active} />
      ) : (
        <s-stack gap="tight">
          {rows.map((r) => <ProductRow key={r.productId} row={r} />)}
        </s-stack>
      )}
    </s-page>
  );
}

function EmptyState({ tab, pendingApprovals, pausedCount, activeCount }) {
  if (tab === "paused") {
    return (
      <s-section>
        <s-stack gap="tight">
          <s-text emphasis="bold">No paused products.</s-text>
          <s-text tone="subdued">
            Confirmed products with auto-pricing turned off appear here.
            You currently have <strong>{activeCount}</strong> on the Active tab.
          </s-text>
        </s-stack>
      </s-section>
    );
  }
  // tab === "active"
  return (
    <s-section>
      <s-stack gap="base">
        <s-text emphasis="bold">No products matched perfectly.</s-text>
        <s-text tone="subdued">
          A product appears here once it has a confirmed competitor match and
          auto-pricing is on for at least one of its variants.
        </s-text>
        {pausedCount > 0 ? (
          <s-text>
            You have <strong>{pausedCount}</strong> paused product
            {pausedCount === 1 ? "" : "s"} you can turn back on from the{" "}
            <strong>Paused</strong> tab above.
          </s-text>
        ) : null}
        {pendingApprovals > 0 ? (
          <s-text>
            You have <strong>{pendingApprovals}</strong> pending match
            {pendingApprovals === 1 ? "" : "es"} waiting for your approval.{" "}
            <Link to="/app/matches">Go to Matched competitors →</Link>
          </s-text>
        ) : pausedCount === 0 ? (
          <s-text tone="subdued">
            No matches are awaiting approval either. Run a competitor scrape
            from the Controller, then come back once new candidates appear.
          </s-text>
        ) : null}
      </s-stack>
    </s-section>
  );
}

// Collapsed row = my product only. Expand to see competitors, price history,
// and per-variant auto-pricing toggles.
function ProductRow({ row }) {
  const [expanded, setExpanded] = useState(false);
  const fetcher = useFetcher();
  const busy = fetcher.state !== "idle";

  const priceLabel = row.minPrice == null
    ? "—"
    : row.minPrice === row.maxPrice
      ? `₹${row.minPrice.toFixed(2)}`
      : `₹${row.minPrice.toFixed(2)} – ₹${row.maxPrice.toFixed(2)}`;

  return (
    <s-section>
      {/* ── Collapsed header (always visible) ─────────────────────────── */}
      <s-stack direction="inline" gap="base" alignment="space-between">
        <s-stack direction="inline" gap="tight" style={{ flex: 2 }}>
          {row.imageUrl ? (
            <img src={row.imageUrl} alt=""
                 style={{ width: 64, height: 64, objectFit: "contain",
                          borderRadius: 6, background: "#f6f6f7" }} />
          ) : null}
          <s-stack gap="tight">
            <s-text emphasis="bold">{row.title}</s-text>
            <s-text tone="subdued" style={{ fontSize: 12 }}>
              {row.vendor || "—"} · {row.activeVariants} of {row.totalVariants}
              {" "}variant{row.totalVariants === 1 ? "" : "s"} auto-priced
            </s-text>
            <s-text tone="subdued" style={{ fontSize: 12 }}>
              Current price: <strong>{priceLabel}</strong>
              {row.competitors.length
                ? ` · tracking ${row.competitors.length} competitor${row.competitors.length === 1 ? "" : "s"}`
                : ""}
            </s-text>
          </s-stack>
        </s-stack>

        <s-stack gap="tight" alignment="end" style={{ flex: 1 }}>
          <s-text tone="subdued" style={{ fontSize: 12 }}>
            {row.lastApplied
              ? <>Last priced ₹{row.lastApplied.oldPrice.toFixed(2)} → ₹{row.lastApplied.newPrice.toFixed(2)} · {fmtTimeAgo(row.lastApplied.appliedAt)}</>
              : "No prices pushed yet."}
          </s-text>
          <s-button variant="secondary" onClick={() => setExpanded(!expanded)}>
            {expanded ? "Collapse ▲" : "Expand ▼"}
          </s-button>
        </s-stack>
      </s-stack>

      {/* ── Expanded panel ────────────────────────────────────────────── */}
      {expanded ? (
        <div style={{ marginTop: 12, borderTop: "1px solid #e4e5e7", paddingTop: 12 }}>
          <s-stack gap="base">
            <CompetitorsPanel competitors={row.competitors} yourMin={row.minPrice} />
            <VariantsPanel variants={row.variants} productId={row.productId}
                           fetcher={fetcher} busy={busy} />
            <ProductControls productId={row.productId} hasActive={row.activeVariants > 0}
                             fetcher={fetcher} busy={busy} />
          </s-stack>
        </div>
      ) : null}
    </s-section>
  );
}

function CompetitorsPanel({ competitors, yourMin }) {
  if (!competitors.length) {
    return <s-text tone="subdued" style={{ fontSize: 12 }}>No competitors mapped to this product.</s-text>;
  }
  return (
    <s-stack gap="tight">
      <s-text emphasis="bold" style={{ fontSize: 13 }}>
        Tracked competitors ({competitors.length})
      </s-text>
      <s-stack gap="tight">
        {competitors.map((c) => {
          const delta = (yourMin != null && c.minPrice != null) ? c.minPrice - yourMin : null;
          const cheaper = delta != null && delta < -0.005;
          const dearer  = delta != null && delta >  0.005;
          return (
            <s-stack key={c.matchId} direction="inline" gap="tight" alignment="space-between"
              style={{ padding: "6px 10px", background: "#fafbfb", borderRadius: 6, fontSize: 12 }}>
              <s-stack direction="inline" gap="tight">
                {c.imageUrl ? (
                  <img src={c.imageUrl} alt=""
                       style={{ width: 32, height: 32, objectFit: "contain",
                                borderRadius: 4, background: "#fff" }} />
                ) : null}
                <s-stack gap="tight">
                  <s-text>{c.title}</s-text>
                  <s-text tone="subdued" style={{ fontSize: 11 }}>
                    {c.domain}
                    {c.confidence != null ? ` · match ${(c.confidence * 100).toFixed(0)}%` : ""}
                    {c.inStock === false ? " · out of stock" : ""}
                  </s-text>
                </s-stack>
              </s-stack>
              <s-text>
                {c.minPrice != null ? `₹${c.minPrice.toFixed(2)}` : "—"}
                {delta != null ? (
                  <span style={{
                    marginLeft: 8,
                    color: cheaper ? "#bf0711" : dearer ? "#0a5a2a" : "#6d7175",
                  }}>
                    ({cheaper ? "↓" : dearer ? "↑" : "·"} ₹{Math.abs(delta).toFixed(2)} vs you)
                  </span>
                ) : null}
              </s-text>
            </s-stack>
          );
        })}
      </s-stack>
    </s-stack>
  );
}

function VariantsPanel({ variants, fetcher, busy }) {
  return (
    <s-stack gap="tight">
      <s-text emphasis="bold" style={{ fontSize: 13 }}>
        Variants & price history
      </s-text>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr style={{ background: "#fafbfb", textAlign: "left" }}>
            <th style={th}>Variant</th>
            <th style={th}>Current price</th>
            <th style={th}>Recent price changes</th>
            <th style={th}>Auto-pricing</th>
          </tr>
        </thead>
        <tbody>
          {variants.map((v) => (
            <tr key={v.id} style={{ borderTop: "1px solid #e4e5e7" }}>
              <td style={td}>
                <Link to={`/app/pricing/${encodeURIComponent(v.id)}`}>{v.title}</Link>
                {v.inventory != null ? (
                  <div style={{ color: "#6d7175", fontSize: 11 }}>{v.inventory} in stock</div>
                ) : null}
              </td>
              <td style={td}>₹{v.currentPrice.toFixed(2)}</td>
              <td style={td}>
                {v.history.length === 0
                  ? <span style={{ color: "#6d7175" }}>no changes yet</span>
                  : (
                    <s-stack gap="tight">
                      {v.history.map((h) => (
                        <div key={h.id} style={{ fontSize: 11 }}>
                          <span style={{ color: h.reverted ? "#6d7175" : "#0a0a0a" }}>
                            ₹{h.oldPrice.toFixed(2)} → ₹{h.newPrice.toFixed(2)}
                          </span>
                          <span style={{ color: "#6d7175", marginLeft: 6 }}>
                            {fmtTimeAgo(h.appliedAt)}
                            {h.reverted ? " · reverted" : ""}
                            {h.mlPrice != null ? ` · ML said ₹${h.mlPrice.toFixed(2)}` : ""}
                          </span>
                        </div>
                      ))}
                    </s-stack>
                  )}
              </td>
              <td style={td}>
                <fetcher.Form method="post">
                  <input type="hidden" name="intent" value="toggleVariantAutoPrice" />
                  <input type="hidden" name="variantId" value={v.id} />
                  <input type="hidden" name="enabled" value={String(!v.autoPriceEnabled)} />
                  <s-button type="submit"
                            variant={v.autoPriceEnabled ? "primary" : "secondary"}
                            disabled={busy}>
                    {v.autoPriceEnabled ? "ON" : "OFF"}
                  </s-button>
                </fetcher.Form>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </s-stack>
  );
}

function ProductControls({ productId, hasActive, fetcher, busy }) {
  return (
    <s-stack direction="inline" gap="tight" alignment="end">
      <fetcher.Form method="post">
        <input type="hidden" name="intent" value="toggleProductAutoPrice" />
        <input type="hidden" name="productId" value={productId} />
        <input type="hidden" name="enabled" value={String(!hasActive)} />
        <s-button type="submit" tone={hasActive ? "critical" : "default"}
                  variant="secondary" disabled={busy}>
          {hasActive ? "Turn OFF dynamic pricing (all variants)" : "Turn ON dynamic pricing (all variants)"}
        </s-button>
      </fetcher.Form>
    </s-stack>
  );
}

const th = { padding: "6px 8px", fontWeight: 600, fontSize: 12, color: "#3d3f44" };
const td = { padding: "8px", verticalAlign: "top" };

function fmtTimeAgo(d) {
  if (!d) return "";
  const ms = Date.now() - new Date(d).getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 1)  return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

export function ErrorBoundary() {
  return boundary.error(useRouteError());
}
