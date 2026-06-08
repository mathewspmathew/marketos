import { useState } from "react";
import PropTypes from "prop-types";
import QueryStudioPanel from "./QueryStudioPanel";

const clamp = (v, lo, hi, dflt) => {
  const n = parseInt(v, 10);
  if (Number.isNaN(n)) return dflt;
  return Math.max(lo, Math.min(n, hi));
};

export default function DynamicPricingCard({ preview, onApply, onCancel, busy = false }) {
  const change = preview.change || {};
  const summary = preview.summary || {};
  const enable = !!change.enabled;

  // Enable-form local state, pre-filled from the frozen preview defaults.
  const [rescrape, setRescrape] = useState(false);
  const [num, setNum] = useState(String(change.numResults ?? 10));
  const [cap, setCap] = useState(String(change.listingExpansionCap ?? 5));
  // Disable-form local state. Default = pause (keep data).
  const [mode, setMode] = useState("pause");

  const count = summary.count ?? (preview.variantIds?.length ?? 0);
  const dc = summary.deleteCounts || {};

  // Search query the merchant can edit or pick via Query Studio (enable only).
  const [query, setQuery] = useState(change.query ?? "");
  const [showStudio, setShowStudio] = useState(false);
  // For toggle previews, variantIds holds product ids; sampleRows carry titles.
  const productId = (preview.variantIds && preview.variantIds[0]) || "";
  const productTitle = summary.sampleRows?.[0]?.title || "";

  const apply = () => {
    if (enable) {
      onApply(preview, {
        enable: true,
        rescrape,
        numResults: clamp(num, 1, 50, 10),
        listingExpansionCap: clamp(cap, 1, 50, 5),
        query: query.trim(),
      });
    } else {
      onApply(preview, { enable: false, mode });
    }
  };

  return (
    <s-section>
      <s-stack direction="block" gap="base">
        <s-text emphasis="bold">
          {enable ? "Turn on dynamic pricing" : "Turn off dynamic pricing"}
        </s-text>
        <s-text>{count} {count === 1 ? "product" : "products"}</s-text>

        {enable ? (
          <s-stack direction="block" gap="base">
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
            <s-text tone="subdued">
              The numbers above set how wide this product&apos;s first
              competitor fetch goes — they apply either way. Choose
              &quot;Now&quot; to start immediately, or &quot;Shortly&quot; to
              let the scheduler run it within a minute.
            </s-text>
            <s-text-field
              label="Search query (used to find competitors)"
              value={query}
              onInput={(e) => setQuery(e.currentTarget.value)}
            />
            <s-button onClick={() => setShowStudio((v) => !v)}>
              {showStudio ? "Hide Query Studio" : "Find a better query"}
            </s-button>
            {showStudio && productId && (
              <QueryStudioPanel
                productId={productId}
                productTitle={productTitle}
                onUse={(q) => { setQuery(q); setShowStudio(false); }}
              />
            )}
          </s-stack>
        ) : (
          <s-select
            label="When turning off"
            value={mode}
            onChange={(e) => setMode(e.currentTarget.value)}
          >
            <s-option value="pause">Pause tracking (keep data)</s-option>
            <s-option value="delete">
              {`Turn off & delete competitor data — ${dc.competitor_products ?? 0} products, ${dc.discovered_links ?? 0} links`}
            </s-option>
          </s-select>
        )}

        <s-stack direction="inline" gap="base">
          <s-button variant="primary" loading={busy || undefined} onClick={apply}>
            Continue
          </s-button>
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
