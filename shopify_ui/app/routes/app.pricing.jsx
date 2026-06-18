/* eslint-disable react/prop-types */
import { useFetcher, useLoaderData, useRouteError, Link, useSearchParams } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

const FILTERS = [
  { key: "live",     label: "Auto-priced now" },     // default — the "what's actively changing" view
  { key: "eligible", label: "Eligible (has CONFIRMED match)" },
  { key: "active",   label: "Auto-pricing on" },
  { key: "blocked",  label: "Last decision blocked" },
  { key: "paused",   label: "Auto-pricing off" },
  { key: "all",      label: "All variants" },
];

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const url = new URL(request.url);
  const filter = url.searchParams.get("filter") || "live";

  // All variants for this shop, plus product context.
  const variants = await db.shopifyVariant.findMany({
    where: { ShopifyProduct: { shopDomain: shop } },
    orderBy: [{ ShopifyProduct: { title: "asc" } }, { title: "asc" }],
    select: {
      id: true,
      title: true,
      currentPrice: true,
      autoPriceEnabled: true,
      inventoryQuantity: true,
      imageUrl: true,
      ShopifyProduct: {
        select: {
          id: true, title: true, vendor: true, imageUrl: true,
          ProductLevelMatch: {
            where: { confidenceTier: "CONFIRMED" },
            select: { id: true },
            take: 1,
          },
        },
      },
      // Most-recent decision (any state) for the snapshot row.
      PriceDecision: {
        orderBy: { decidedAt: "desc" },
        take: 2,
        select: {
          decidedAt: true, appliedAt: true, revertedAt: true,
          oldPrice: true, newPrice: true,
          reason: true, blockedBy: true, confidence: true,
          ruleSuggestedPrice: true,
        },
      },
      VariantCompetitorStats: {
        select: { competitorCount: true, weightedMin: true, weightedMedian: true,
                  lastUpdatedAt: true },
      },
      // Mapped competitors for the "what are we pricing against?" panel.
      // Confidence-ordered so the most trustworthy mappings show first;
      // dismissed/unbound rows are filtered out at the SQL level.
      ProductMatch: {
        where: { competitorVariantId: { not: null }, dismissedAt: null },
        orderBy: [{ confidence: "desc" }, { matchScore: "desc" }],
        take: 5,
        select: {
          id: true,
          confidenceTier: true,
          confidence: true,
          ScrapedVariant: {
            select: {
              id: true, title: true, currentPrice: true,
              ScrapedProduct: { select: { title: true, domain: true } },
            },
          },
        },
      },
    },
  });

  const rows = variants.map((v) => {
    const lastDec = v.PriceDecision[0] || null;
    // Most-recent decision that actually got pushed to Shopify (replaces
    // the old PriceChange lookup — appliedAt != NULL is the signal).
    const lastApplied = v.PriceDecision.find((d) => d.appliedAt) || null;
    const hasConfirmed = v.ShopifyProduct.ProductLevelMatch.length > 0;
    return {
      id:                v.id,
      title:             v.title,
      productTitle:      v.ShopifyProduct.title,
      productVendor:     v.ShopifyProduct.vendor,
      imageUrl:          v.imageUrl || v.ShopifyProduct.imageUrl,
      currentPrice:      Number(v.currentPrice),
      autoPriceEnabled:  v.autoPriceEnabled,      useMlSuggestion:   v.useMlSuggestion,
      inventoryQuantity: v.inventoryQuantity,
      hasConfirmed,
      lastDecision: lastDec ? {
        ...lastDec,
        oldPrice:           Number(lastDec.oldPrice),
        newPrice:           Number(lastDec.newPrice),
        ruleSuggestedPrice: lastDec.ruleSuggestedPrice != null
          ? Number(lastDec.ruleSuggestedPrice) : null,
        mlSuggestedPrice:   lastDec.mlSuggestedPrice != null
          ? Number(lastDec.mlSuggestedPrice) : null,
        mlConfidence:       lastDec.mlConfidence != null
          ? Number(lastDec.mlConfidence) : null,
        modelVersion:       lastDec.modelVersion ?? null,
        confidence:         Number(lastDec.confidence),
      } : null,
      lastChange: lastApplied ? {
        appliedAt:  lastApplied.appliedAt,
        oldPrice:   Number(lastApplied.oldPrice),
        newPrice:   Number(lastApplied.newPrice),
        revertedAt: lastApplied.revertedAt,
      } : null,
      stats: v.VariantCompetitorStats ? {
        competitorCount: v.VariantCompetitorStats.competitorCount,
        weightedMin:     v.VariantCompetitorStats.weightedMin != null
          ? Number(v.VariantCompetitorStats.weightedMin) : null,
        weightedMedian:  v.VariantCompetitorStats.weightedMedian != null
          ? Number(v.VariantCompetitorStats.weightedMedian) : null,
        lastUpdatedAt:   v.VariantCompetitorStats.lastUpdatedAt,
      } : null,
      mappedCompetitors: v.ProductMatch
        .filter((m) => m.ScrapedVariant)
        .map((m) => ({
          id:             m.id,
          tier:           m.confidenceTier,
          confidence:     m.confidence != null ? Number(m.confidence) : null,
          title:          m.ScrapedVariant.ScrapedProduct.title,
          variantTitle:   m.ScrapedVariant.title !== "Default Title"
                            ? m.ScrapedVariant.title : null,
          domain:         m.ScrapedVariant.ScrapedProduct.domain,
          competitorPrice: m.ScrapedVariant.currentPrice != null
                            ? Number(m.ScrapedVariant.currentPrice) : null,
        })),
    };
  });

  const isLive = (r) => r.autoPriceEnabled && r.hasConfirmed;
  const filtered = rows.filter((r) => {
    if (filter === "live")     return isLive(r);
    if (filter === "active")   return r.autoPriceEnabled;
    if (filter === "eligible") return r.hasConfirmed;
    if (filter === "blocked")  return r.lastDecision?.blockedBy != null;
    if (filter === "paused")   return !r.autoPriceEnabled;
    return true;
  });

  const counts = {
    all:      rows.length,
    live:     rows.filter(isLive).length,
    active:   rows.filter((r) => r.autoPriceEnabled).length,
    eligible: rows.filter((r) => r.hasConfirmed).length,
    blocked:  rows.filter((r) => r.lastDecision?.blockedBy != null).length,
    paused:   rows.filter((r) => !r.autoPriceEnabled).length,
  };

  return { shop, filter, rows: filtered, counts };
};

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const fd = await request.formData();
  const intent = fd.get("intent");

  if (intent === "toggleAutoPrice") {
    const id = String(fd.get("id"));
    const enabled = fd.get("enabled") === "true";
    await db.shopifyVariant.updateMany({
      where: { id, ShopifyProduct: { shopDomain: shop } },
      data:  { autoPriceEnabled: enabled },
    });
    return { ok: true };
  }
  if (intent === "toggleMlOptIn") {
    const id = String(fd.get("id"));
    const enabled = fd.get("enabled") === "true";
    await db.shopifyVariant.updateMany({
      where: { id, ShopifyProduct: { shopDomain: shop } },
      data:  { useMlSuggestion: enabled },
    });
    return { ok: true };
  }
  return { ok: false, error: "unknown_intent" };
};

export default function PricingCatalog() {
  const { rows, filter, counts } = useLoaderData();
  const [searchParams, setSearchParams] = useSearchParams();

  return (
    <s-page heading="Pricing catalog">
      <s-section>
        <s-stack gap="tight">
          <s-text>
            Each variant shows three prices side-by-side so you can compare
            before letting auto-pricing ship a change.
          </s-text>
          <s-stack direction="inline" gap="base">
            <s-text tone="subdued" style={{ fontSize: 12 }}>
              <strong>Rule says</strong> — what your competitor-based rule
              suggests (e.g. match the lowest competitor minus 2%).
            </s-text>
            <s-text tone="subdued" style={{ fontSize: 12 }}>
              <strong>ML says</strong> — what the demand model predicts is
              the best price. Shown for comparison even when off.
            </s-text>
            <s-text tone="subdued" style={{ fontSize: 12 }}>
              <strong>Would ship</strong> — the final price (rule-only by
              default, or a rule+ML blend when you opt in per variant).
            </s-text>
          </s-stack>
        </s-stack>
      </s-section>

      <s-section>
        <s-stack direction="inline" gap="tight">
          {FILTERS.map((f) => (
            <s-button
              key={f.key}
              variant={filter === f.key ? "primary" : "secondary"}
              onClick={() => {
                const next = new URLSearchParams(searchParams);
                next.set("filter", f.key);
                setSearchParams(next);
              }}
            >
              {f.label} ({counts[f.key] ?? 0})
            </s-button>
          ))}
        </s-stack>
      </s-section>

      {rows.length === 0 ? (
        <s-section>
          <s-text>No variants in this view.</s-text>
        </s-section>
      ) : (
        <s-stack gap="tight">
          {rows.map((r) => <VariantRow key={r.id} row={r} />)}
        </s-stack>
      )}
    </s-page>
  );
}

// Friendly explanations for the engine's internal block codes. These are
// what the merchant sees in the row — the raw blockedBy string is kept
// in the decision row for debugging.
const BLOCK_REASONS = {
  no_rule:             { label: "No pricing rule set",       hint: "Add a rule for this shop / product / variant to start pricing." },
  kill_switch:         { label: "Kill switch is on",          hint: "Auto-pricing is paused for the whole shop. Turn it off in settings." },
  no_confirmed_match:  { label: "No confirmed competitor",    hint: "Confirm at least one matched competitor product before auto-pricing can run." },
  stale_data:          { label: "Competitor data is stale",   hint: "We haven't seen fresh competitor prices recently. A re-scrape will unblock this." },
  active_promotion:    { label: "Promotion window active",    hint: "Auto-pricing is paused while your promotion is live." },
  no_candidate:        { label: "Rule couldn't suggest a price", hint: "Usually means no competitor data matched the rule's filters." },
  noop:                { label: "No change needed",           hint: "The rule's recommended price equals the current price." },
};

function describeBlock(code) {
  return BLOCK_REASONS[code] || { label: code, hint: null };
}

// Compact price comparison: Rule | ML | Final. The "Final" is what would
// (or did) ship to Shopify; the others are shown side-by-side so the
// merchant can see where each input landed.
function PriceCompare({ rule, ml, mlConf, mlVersion, final, current }) {
  const Col = ({ label, value, sub, tone }) => (
    <div style={{
      flex: 1, padding: "8px 10px", borderRadius: 6,
      border: "1px solid #e4e5e7",
      background: tone === "primary" ? "#eef5ff" : "#fafbfb",
    }}>
      <div style={{ fontSize: 11, color: "#6d7175", marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 600,
                    color: tone === "primary" ? "#1f3a8a" : "#0a0a0a" }}>
        {value}
      </div>
      {sub ? <div style={{ fontSize: 11, color: "#6d7175", marginTop: 2 }}>{sub}</div> : null}
    </div>
  );
  return (
    <s-stack direction="inline" gap="tight">
      <Col label="Rule says" value={rule != null ? `₹${rule.toFixed(2)}` : "—"}
           sub="competitor-based"
           tone={final != null && rule != null && Math.abs(final - rule) < 0.005 ? "primary" : null} />
      <Col label="ML says"
           value={ml != null ? `₹${ml.toFixed(2)}` : "—"}
           sub={ml != null ? `conf ${(mlConf * 100).toFixed(0)}%${mlVersion ? ` · ${mlVersion}` : ""}` : "no model yet"} />
      <Col label="Would ship" value={final != null ? `₹${final.toFixed(2)}` : "—"}
           sub={current != null && final != null && Math.abs(final - current) < 0.005 ? "= current" : "Δ vs current"}
           tone="primary" />
    </s-stack>
  );
}

function VariantRow({ row }) {
  const fetcher = useFetcher();
  const busy = fetcher.state !== "idle";
  const decision = row.lastDecision;
  const change   = row.lastChange;
  const block    = decision?.blockedBy ? describeBlock(decision.blockedBy) : null;

  return (
    <s-section>
      <s-stack gap="tight">
        {/* ── Top row: product + controls ─────────────────────────────── */}
        <s-stack direction="inline" gap="base" alignment="space-between">
          <s-stack direction="inline" gap="tight" style={{ flex: 2 }}>
            {row.imageUrl ? (
              <img src={row.imageUrl} alt=""
                   style={{ width: 56, height: 56, objectFit: "contain",
                            borderRadius: 6, background: "#f6f6f7" }} />
            ) : null}
            <s-stack gap="tight">
              <s-text emphasis="bold">{row.productTitle}</s-text>
              <s-text tone="subdued">
                {row.title}{row.productVendor ? ` · ${row.productVendor}` : ""}
              </s-text>
              <s-stack direction="inline" gap="tight">
                <Badge tone={row.hasConfirmed ? "success" : "subdued"}>
                  {row.hasConfirmed ? "Confirmed match ✓" : "No confirmed match"}
                </Badge>
                {row.inventoryQuantity != null ? (
                  <Badge tone="subdued">{row.inventoryQuantity} in stock</Badge>
                ) : null}
                <Badge tone="subdued">Current ₹{row.currentPrice.toFixed(2)}</Badge>
              </s-stack>
            </s-stack>
          </s-stack>

          <s-stack gap="tight" alignment="end" style={{ flex: 1 }}>
            <fetcher.Form method="post">
              <input type="hidden" name="intent" value="toggleAutoPrice" />
              <input type="hidden" name="id" value={row.id} />
              <input type="hidden" name="enabled" value={String(!row.autoPriceEnabled)} />
              <s-button
                type="submit"
                variant={row.autoPriceEnabled ? "primary" : "secondary"}
                disabled={busy || !row.hasConfirmed}
              >
                {row.autoPriceEnabled ? "Auto-pricing: ON" : "Enable auto-pricing"}
              </s-button>
            </fetcher.Form>

            <fetcher.Form method="post">
              <input type="hidden" name="intent" value="toggleMlOptIn" />
              <input type="hidden" name="id" value={row.id} />
              <input type="hidden" name="enabled" value={String(!row.useMlSuggestion)} />
              <s-button
                type="submit"
                variant={row.useMlSuggestion ? "primary" : "secondary"}
                disabled={busy}
              >
                {row.useMlSuggestion
                  ? "ML blending: ON"
                  : "Let ML influence price"}
              </s-button>
            </fetcher.Form>

            <s-text tone="subdued" style={{ fontSize: 11 }}>
              {row.useMlSuggestion
                ? "Final price is a mix of rule + ML prediction."
                : "Final price is rule-only. ML shown for comparison."}
            </s-text>
          </s-stack>
        </s-stack>

        {/* ── Price comparison strip ──────────────────────────────────── */}
        {decision ? (
          <PriceCompare
            rule={decision.ruleSuggestedPrice}
            ml={decision.mlSuggestedPrice}
            mlConf={decision.mlConfidence}
            mlVersion={decision.modelVersion}
            final={decision.newPrice}
            current={row.currentPrice}
          />
        ) : (
          <s-text tone="subdued">No pricing decision has been computed yet.</s-text>
        )}

        {/* ── Tracked competitors panel ───────────────────────────────── */}
        {row.mappedCompetitors.length > 0 ? (
          <CompetitorPanel competitors={row.mappedCompetitors}
                           yourPrice={row.currentPrice} />
        ) : null}

        {/* ── Decision status + competitor context ────────────────────── */}
        <s-stack direction="inline" gap="base" alignment="space-between">
          <s-stack gap="tight" style={{ flex: 1 }}>
            {decision ? (
              <s-text tone="subdued" style={{ fontSize: 12 }}>
                {block ? (
                  <>
                    <span style={{ color: "#b9852c", fontWeight: 600 }}>⚠ {block.label}</span>
                    {block.hint ? <> — {block.hint}</> : null}
                  </>
                ) : (
                  <>
                    Decision <strong>{fmtTimeAgo(decision.decidedAt)}</strong>
                    {" · "}competitor-signal confidence <strong>{(decision.confidence * 100).toFixed(0)}%</strong>
                  </>
                )}
              </s-text>
            ) : null}
            {change ? (
              <s-text tone="subdued" style={{ fontSize: 12 }}>
                Last pushed to Shopify: ₹{change.oldPrice.toFixed(2)} → ₹{change.newPrice.toFixed(2)}
                {" · "}{fmtTimeAgo(change.appliedAt)}
                {change.revertedAt ? " · reverted" : ""}
              </s-text>
            ) : (
              <s-text tone="subdued" style={{ fontSize: 12 }}>
                No price has been pushed to Shopify yet.
              </s-text>
            )}
          </s-stack>

          <s-stack gap="tight" alignment="end" style={{ flex: 1 }}>
            {row.stats ? (
              <s-text tone="subdued" style={{ fontSize: 12 }}>
                Competitors: <strong>{row.stats.competitorCount ?? 0}</strong>
                {row.stats.weightedMin != null ?
                  ` · min ₹${row.stats.weightedMin.toFixed(2)}` : ""}
                {row.stats.weightedMedian != null ?
                  ` · median ₹${row.stats.weightedMedian.toFixed(2)}` : ""}
                {row.stats.lastUpdatedAt
                  ? ` · seen ${fmtTimeAgo(row.stats.lastUpdatedAt)}` : ""}
              </s-text>
            ) : (
              <s-text tone="subdued" style={{ fontSize: 12 }}>
                No competitor data yet.
              </s-text>
            )}
            <Link to={`/app/pricing/${encodeURIComponent(row.id)}`}>
              <s-button variant="secondary">Open history →</s-button>
            </Link>
          </s-stack>
        </s-stack>
      </s-stack>
    </s-section>
  );
}

// Inline summary of the competitors this variant is being priced against.
// Each row: confidence dot · competitor title · domain · their price · Δ vs yours.
function CompetitorPanel({ competitors, yourPrice }) {
  const tierColor = (t) =>
    t === "CONFIRMED" ? "#0a5a2a" :
    t === "LIKELY"    ? "#b9852c" :
    t === "REJECTED"  ? "#bf0711" : "#6d7175";

  return (
    <div style={{ borderTop: "1px dashed #e4e5e7", paddingTop: 8 }}>
      <s-text tone="subdued" style={{ fontSize: 12 }}>
        <strong>Pricing against {competitors.length} competitor{competitors.length === 1 ? "" : "s"}:</strong>
      </s-text>
      <s-stack gap="tight" style={{ marginTop: 4 }}>
        {competitors.map((c) => {
          const delta = c.competitorPrice != null
            ? c.competitorPrice - yourPrice : null;
          const cheaper = delta != null && delta < -0.005;
          const dearer  = delta != null && delta >  0.005;
          return (
            <s-stack key={c.id} direction="inline" gap="tight" alignment="space-between"
              style={{ fontSize: 12 }}>
              <s-stack direction="inline" gap="tight">
                <span title={c.tier ?? "no tier"} style={{
                  display: "inline-block", width: 8, height: 8, borderRadius: 999,
                  background: tierColor(c.tier), marginTop: 5,
                }} />
                <s-text>
                  {c.title}
                  {c.variantTitle ? <span style={{ color: "#6d7175" }}> · {c.variantTitle}</span> : null}
                  <span style={{ color: "#6d7175" }}> · {c.domain}</span>
                </s-text>
              </s-stack>
              <s-text tone="subdued">
                {c.competitorPrice != null ? `₹${c.competitorPrice.toFixed(2)}` : "—"}
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
    </div>
  );
}

function Badge({ tone, children }) {
  const bg = tone === "success" ? "#cde9d6" : "#e4e5e7";
  const fg = tone === "success" ? "#0a5a2a" : "#3d3f44";
  return (
    <span style={{
      background: bg, color: fg,
      padding: "2px 8px", borderRadius: 999, fontSize: 12,
    }}>{children}</span>
  );
}

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
