import { useState } from "react";
import PropTypes from "prop-types";
// Query Studio is deferred to a future update — keep the code, don't render it.
// import QueryStudioPanel from "./QueryStudioPanel";

const clamp = (v, lo, hi, dflt) => {
  const n = parseInt(v, 10);
  if (Number.isNaN(n)) return dflt;
  return Math.max(lo, Math.min(n, hi));
};

export default function DynamicPricingCard({ preview, onApply, onCancel, busy = false }) {
  const change = preview.change || {};
  const summary = preview.summary || {};
  const state = change.cardState || (change.enabled ? "FRESH" : "ACTIVE");
  const product = summary.product || {};
  const ctx = summary.enableContext || {};
  const dc = summary.deleteCounts || {};
  const stats = summary.stats || null;

  // FRESH form state, pre-filled from the frozen preview defaults.
  const [num, setNum] = useState(String(change.numResults ?? 10));
  const [cap, setCap] = useState(String(change.listingExpansionCap ?? 5));
  const [query, setQuery] = useState(change.query ?? "");
  const [rescrape, setRescrape] = useState(false);
  // Query Studio deferred:
  // const [showStudio, setShowStudio] = useState(false);

  const enableNow = () =>
    onApply(preview, {
      action: "enable",
      rescrape,
      numResults: clamp(num, 1, 50, 10),
      listingExpansionCap: clamp(cap, 1, 50, 5),
      query: query.trim(),
    });

  const heading = {
    FRESH: "Set up dynamic pricing",
    ACTIVE: "Dynamic pricing is running",
    PAUSED: "Dynamic pricing is paused",
  }[state];

  return (
    <s-section>
      <s-stack direction="block" gap="base">
        {/* Product header — mirrors the product pane */}
        <s-stack direction="inline" gap="base" align="center">
          {product.imageUrl && (
            <img src={product.imageUrl} alt={product.title} width="48" height="48"
                 style={{ objectFit: "cover", borderRadius: 4 }} />
          )}
          <s-stack direction="block" gap="tight">
            <s-text emphasis="bold">{product.title}</s-text>
            <s-text tone="subdued">
              {product.vendor ? `${product.vendor} · ` : ""}{product.category || ""}
            </s-text>
          </s-stack>
          <s-badge tone={state === "ACTIVE" ? "success" : "subdued"}>{heading}</s-badge>
        </s-stack>

        {state !== "FRESH" && (
          <s-banner tone="info">
            <s-text>
              {`${ctx.competitors_found ?? 0} competitor(s), ${ctx.live_matches ?? 0} matched`}
              {ctx.last_discovery_at
                ? ` · last fetched ${new Date(ctx.last_discovery_at).toLocaleDateString()}`
                : ""}
              {product.latestJobStatus ? ` · last job ${product.latestJobStatus}` : ""}
            </s-text>
          </s-banner>
        )}

        {state === "ACTIVE" && stats && (
          <s-stack direction="block" gap="tight">
            {stats.lastPriceChange && (
              <s-text>
                Current price: <strong>₹{stats.lastPriceChange.newPrice.toFixed(2)}</strong>
                {" "}(was ₹{stats.lastPriceChange.oldPrice.toFixed(2)}, changed{" "}
                {new Date(stats.lastPriceChange.appliedAt).toLocaleDateString()})
              </s-text>
            )}
            {stats.competitors && (
              <s-text tone="subdued">
                {stats.competitors.count} competitor price(s) tracked
                {stats.competitors.minPrice != null
                  ? ` · ₹${stats.competitors.minPrice.toFixed(2)}–₹${stats.competitors.maxPrice.toFixed(2)}`
                  : ""}
                {stats.competitors.median != null ? ` (median ₹${stats.competitors.median.toFixed(2)})` : ""}
              </s-text>
            )}
          </s-stack>
        )}

        {state === "FRESH" && (
          <s-stack direction="block" gap="base">
            <s-text-field
              label="Search query (used to find competitors)"
              value={query}
              onInput={(e) => setQuery(e.currentTarget.value)}
            />
            <s-stack direction="inline" gap="base" align="end">
              <s-text-field
                label="Competitor sites to find"
                type="number" value={num} min="1" max="50"
                onInput={(e) => setNum(e.currentTarget.value)}
              />
              <s-text-field
                label="Max products per listing page"
                type="number" value={cap} min="1" max="50"
                onInput={(e) => setCap(e.currentTarget.value)}
              />
            </s-stack>
            <s-select
              label="When to fetch competitor data"
              value={rescrape ? "yes" : "no"}
              onChange={(e) => setRescrape(e.currentTarget.value === "yes")}
            >
              <s-option value="no">Shortly, in the background</s-option>
              <s-option value="yes">Now</s-option>
            </s-select>
            {/* Query Studio deferred to a future update:
            <s-button onClick={() => setShowStudio((v) => !v)}>
              {showStudio ? "Hide Query Studio" : "Find a better query"}
            </s-button>
            {showStudio && (
              <QueryStudioPanel productId={product.id} productTitle={product.title}
                                onUse={(q) => { setQuery(q); setShowStudio(false); }} />
            )} */}
          </s-stack>
        )}

        {state !== "FRESH" && (
          <s-text tone="subdued">
            Settings can’t be edited here while {state === "ACTIVE" ? "it’s running" : "it’s paused"} —
            use the product page to change the search.
          </s-text>
        )}

        <s-stack direction="inline" gap="base">
          {state === "FRESH" && (
            <s-button variant="primary" loading={busy || undefined} onClick={enableNow}>
              Start tracking
            </s-button>
          )}
          {state === "ACTIVE" && (
            <s-button variant="primary" loading={busy || undefined}
                      onClick={() => onApply(preview, { action: "pause" })}>
              Pause (keep data)
            </s-button>
          )}
          {state === "PAUSED" && (
            <s-button variant="primary" loading={busy || undefined}
                      onClick={() => onApply(preview, { action: "resume" })}>
              Resume
            </s-button>
          )}
          {state !== "FRESH" && (
            <s-button tone="critical" loading={busy || undefined}
                      onClick={() => onApply(preview, { action: "delete" })}>
              {`Turn off & delete data — ${dc.competitor_products ?? 0} products, ${dc.discovered_links ?? 0} links`}
            </s-button>
          )}
          <s-button onClick={onCancel}>Cancel</s-button>
        </s-stack>
      </s-stack>
    </s-section>
  );
}

DynamicPricingCard.propTypes = {
  preview: PropTypes.object.isRequired,
  onApply: PropTypes.func.isRequired,
  onCancel: PropTypes.func.isRequired,
  busy: PropTypes.bool,
};
