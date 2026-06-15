import { useState } from "react";
import { useFetcher, useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import db from "../db.server";
import { authenticate } from "../shopify.server";

// Keep in sync with services/common/frequency.py::CANONICAL_UNITS.
const FREQ_UNITS = [
  { value: "never",  label: "Never (one-time discovery)" },
  { value: "minute", label: "Minutes" },
  { value: "hour",   label: "Hours"   },
  { value: "day",    label: "Days"    },
];
const ALLOWED_UNITS = new Set(FREQ_UNITS.map((u) => u.value));

const DEFAULTS = {
  markupPct: 0.02,
  maxCompetitorsPerProduct: 8,
  frequencyInterval: 1,
  frequencyUnit: "day",
  listingExpansionCap: 5,
  marketplaceBlocklist: [],
  autoRescrapeEnabled: true,
  includeOosInPricing: false,
  // Auto-pricing knobs (per-product overrides live on ShopifyProduct).
  minCompetitorsToPrice: 4,
  topKCompetitors: 4,
  maxAutoApplyChangePct: 0.05,
  lifetimeCapPct: 0.25,
  budgetUndercut: 0.05,
  premiumUplift: 0.05,
  serperGl: "in",
  serperHl: "en",
  serperLocation: "Kochi, Kerala",
};

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  await db.shopifyUser.upsert({
    where: { shopDomain },
    update: {},
    create: { shopDomain },
  });

  const existing = await db.shopSettings.findUnique({ where: { shopDomain } });
  const s = existing ?? (await db.shopSettings.create({
    data: { shopDomain, ...DEFAULTS },
  }));

  return {
    settings: {
      markupPct:                Number(s.markupPct),
      maxCompetitorsPerProduct: s.maxCompetitorsPerProduct,
      frequencyInterval:        s.frequencyInterval,
      frequencyUnit:            s.frequencyUnit,
      listingExpansionCap:      s.listingExpansionCap ?? DEFAULTS.listingExpansionCap,
      marketplaceBlocklist:     s.marketplaceBlocklist ?? [],
      autoRescrapeEnabled:      s.autoRescrapeEnabled ?? true,
      includeOosInPricing:      s.includeOosInPricing ?? false,
      minCompetitorsToPrice:    s.minCompetitorsToPrice ?? DEFAULTS.minCompetitorsToPrice,
      topKCompetitors:          s.topKCompetitors       ?? DEFAULTS.topKCompetitors,
      maxAutoApplyChangePct:    Number(s.maxAutoApplyChangePct ?? DEFAULTS.maxAutoApplyChangePct),
      lifetimeCapPct:           Number(s.lifetimeCapPct        ?? DEFAULTS.lifetimeCapPct),
      budgetUndercut:           Number(s.budgetUndercut        ?? DEFAULTS.budgetUndercut),
      premiumUplift:            Number(s.premiumUplift         ?? DEFAULTS.premiumUplift),
      serperGl:                 s.serperGl       ?? DEFAULTS.serperGl,
      serperHl:                 s.serperHl       ?? DEFAULTS.serperHl,
      serperLocation:           s.serperLocation ?? DEFAULTS.serperLocation,
    },
  };
};

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;
  const formData = await request.formData();

  const parsePctish = (v) => {
    // Accepts "2", "2%", or "0.02" — normalizes to fraction.
    if (v == null || v === "") return null;
    const s = String(v).trim().replace("%", "");
    const n = parseFloat(s);
    if (!Number.isFinite(n) || n < 0) return null;
    return n > 1 ? n / 100 : n;
  };

  const parsePositiveInt = (v, fallback) => {
    const n = parseInt(v, 10);
    return Number.isFinite(n) && n > 0 ? n : fallback;
  };

  const unitRaw = (formData.get("frequencyUnit") || "").toString();
  const unit    = ALLOWED_UNITS.has(unitRaw) ? unitRaw : DEFAULTS.frequencyUnit;

  const blocklistRaw = (formData.get("marketplaceBlocklist") || "").toString();
  const blocklist = blocklistRaw
    .split(/[\n,]+/)
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);

  const data = {
    markupPct: parsePctish(formData.get("markupPct")) ?? DEFAULTS.markupPct,
    maxCompetitorsPerProduct: parsePositiveInt(formData.get("maxCompetitorsPerProduct"), DEFAULTS.maxCompetitorsPerProduct),
    frequencyInterval:        parsePositiveInt(formData.get("frequencyInterval"),        DEFAULTS.frequencyInterval),
    frequencyUnit:            unit,
    listingExpansionCap:      parsePositiveInt(formData.get("listingExpansionCap"),      DEFAULTS.listingExpansionCap),
    marketplaceBlocklist:     { set: blocklist },
    autoRescrapeEnabled:      formData.get("autoRescrapeEnabled") === "true",
    includeOosInPricing:      formData.get("includeOosInPricing") === "true",
    minCompetitorsToPrice:    parsePositiveInt(formData.get("minCompetitorsToPrice"), DEFAULTS.minCompetitorsToPrice),
    topKCompetitors:          parsePositiveInt(formData.get("topKCompetitors"),       DEFAULTS.topKCompetitors),
    maxAutoApplyChangePct:    parsePctish(formData.get("maxAutoApplyChangePct")) ?? DEFAULTS.maxAutoApplyChangePct,
    lifetimeCapPct:           parsePctish(formData.get("lifetimeCapPct"))        ?? DEFAULTS.lifetimeCapPct,
    budgetUndercut:           parsePctish(formData.get("budgetUndercut"))        ?? DEFAULTS.budgetUndercut,
    premiumUplift:            parsePctish(formData.get("premiumUplift"))         ?? DEFAULTS.premiumUplift,
    serperGl:       ((formData.get("serperGl")       || "").toString().trim().toLowerCase()) || DEFAULTS.serperGl,
    serperHl:       ((formData.get("serperHl")       || "").toString().trim().toLowerCase()) || DEFAULTS.serperHl,
    serperLocation: ((formData.get("serperLocation") || "").toString().trim())               || DEFAULTS.serperLocation,
  };

  // Detect OFF → ON transition on the global auto-rescrape switch so we can
  // re-arm stale schedules afterwards.
  const prior = await db.shopSettings.findUnique({
    where: { shopDomain },
    select: { autoRescrapeEnabled: true },
  });
  const priorAutoRescrape = prior?.autoRescrapeEnabled ?? true;
  const autoRescrapeTurnedOn = !priorAutoRescrape && data.autoRescrapeEnabled === true;

  await db.shopSettings.upsert({
    where: { shopDomain },
    update: data,
    create: { shopDomain, ...data, marketplaceBlocklist: blocklist },
  });

  if (autoRescrapeTurnedOn) {
    // Resume the rescrape loop for every product that's still opted in with
    // a real frequency. Beat will pick them up on the next tick.
    await db.productUrl.updateMany({
      where: {
        shopDomain,
        status: "ACTIVE",
        OR: [{ nextRunAt: null }, { nextRunAt: { lte: new Date() } }],
        shopifyProduct: {
          dynamicPricingEnabled: true,
          frequencyUnit: { not: "never" },
        },
      },
      data: { nextRunAt: new Date() },
    });
  }

  return { ok: true };
};

const INFO_BUTTON_STYLE = { display: "inline-flex", alignItems: "center", marginLeft: "4px", cursor: "help" };
const TOOLTIP_STYLE = { fontSize: "0.75em", color: "#666", display: "block", marginTop: "4px" };

function InfoButton({ children }) {
  const [show, setShow] = useState(false);
  return (
    <span style={INFO_BUTTON_STYLE} onMouseEnter={() => setShow(true)} onMouseLeave={() => setShow(false)}>
      <s-icon-button kind="secondary" size="small" icon="help">
        {show && <div style={TOOLTIP_STYLE}>{children}</div>}
      </s-icon-button>
    </span>
  );
}

export default function SettingsPage() {
  const { settings } = useLoaderData();
  const fetcher = useFetcher();
  const saved = fetcher.data?.ok && fetcher.state === "idle";

  const [form, setForm] = useState({
    serperLocation: settings.serperLocation,
    serperGl: settings.serperGl,
    serperHl: settings.serperHl,
    maxCompetitorsPerProduct: String(settings.maxCompetitorsPerProduct),
    listingExpansionCap: String(settings.listingExpansionCap),
    marketplaceBlocklist: (settings.marketplaceBlocklist ?? []).join("\n"),
    markupPct: String(settings.markupPct),
    budgetUndercut: String(settings.budgetUndercut),
    premiumUplift: String(settings.premiumUplift),
    minCompetitorsToPrice: String(settings.minCompetitorsToPrice),
    topKCompetitors: String(settings.topKCompetitors),
    maxAutoApplyChangePct: String(settings.maxAutoApplyChangePct),
    lifetimeCapPct: String(settings.lifetimeCapPct),
    frequencyInterval: String(settings.frequencyInterval),
    frequencyUnit: settings.frequencyUnit,
    autoRescrapeEnabled: settings.autoRescrapeEnabled,
    includeOosInPricing: settings.includeOosInPricing,
  });

  const setField = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));

  const submit = () => {
    fetcher.submit(
      {
        markupPct: form.markupPct,
        maxCompetitorsPerProduct: form.maxCompetitorsPerProduct,
        frequencyInterval: form.frequencyInterval,
        frequencyUnit: form.frequencyUnit,
        listingExpansionCap: form.listingExpansionCap,
        marketplaceBlocklist: form.marketplaceBlocklist,
        autoRescrapeEnabled: String(form.autoRescrapeEnabled),
        includeOosInPricing: String(form.includeOosInPricing),
        minCompetitorsToPrice: form.minCompetitorsToPrice,
        topKCompetitors: form.topKCompetitors,
        maxAutoApplyChangePct: form.maxAutoApplyChangePct,
        lifetimeCapPct: form.lifetimeCapPct,
        budgetUndercut: form.budgetUndercut,
        premiumUplift: form.premiumUplift,
        serperGl: form.serperGl,
        serperHl: form.serperHl,
        serperLocation: form.serperLocation,
      },
      { method: "POST" },
    );
  };

  return (
    <s-page heading="Shop settings">
      {/* 1. How to Find Competitors */}
      <s-section heading="🔍 How to Find Competitors">
        <s-text tone="subdued" style={{ marginBottom: "12px", display: "block" }}>
          Controls where we search for competitors and how many we track.
        </s-text>
        <s-stack direction="block" gap="base">
          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Search location</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  <strong>Market location for competitor search.</strong> More precise = more relevant results. E.g., "Kochi, Kerala" vs. "India".
                </div>
              </s-popover>
            </div>
            <s-text-field
              value={form.serperLocation}
              onInput={(e) => setField("serperLocation", e.currentTarget.value)}
              helpText="Most precise: city, state, country. E.g., Kochi, Kerala or Mumbai, India."
            />
          </div>

          <div style={{ display: "flex", gap: "12px" }}>
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
                <label style={{ fontWeight: "500", fontSize: "14px" }}>Country code</label>
                <s-popover preferredPosition="above">
                  <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "14px" }}>ⓘ</button>
                  <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                    2-letter country code (e.g., in, us, gb, ae). Falls back to this if location is vague.
                  </div>
                </s-popover>
              </div>
              <s-text-field
                value={form.serperGl}
                onInput={(e) => setField("serperGl", e.currentTarget.value)}
                helpText="E.g., in, us, gb, ae"
              />
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
                <label style={{ fontWeight: "500", fontSize: "14px" }}>Language</label>
                <s-popover preferredPosition="above">
                  <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "14px" }}>ⓘ</button>
                  <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                    2-letter language code (e.g., en, hi, ar, de). Filters results by language.
                  </div>
                </s-popover>
              </div>
              <s-text-field
                value={form.serperHl}
                onInput={(e) => setField("serperHl", e.currentTarget.value)}
                helpText="E.g., en, hi, ar, de"
              />
            </div>
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Track up to N competitors per product</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  Maximum number to monitor per product. More = broader coverage but slower scraping. Balance coverage vs. cost.
                </div>
              </s-popover>
            </div>
            <s-text-field
              type="number"
              value={form.maxCompetitorsPerProduct}
              onInput={(e) => setField("maxCompetitorsPerProduct", e.currentTarget.value)}
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Products per search result page</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  When we find a category page, extract this many product cards. Higher = broader but slower.
                </div>
              </s-popover>
            </div>
            <s-text-field
              type="number"
              value={form.listingExpansionCap}
              onInput={(e) => setField("listingExpansionCap", e.currentTarget.value)}
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Exclude these marketplaces</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  Domains to skip during discovery. E.g., amazon.in, ebay.com (one per line).
                </div>
              </s-popover>
            </div>
            <s-textarea
              rows={5}
              value={form.marketplaceBlocklist}
              onInput={(e) => setField("marketplaceBlocklist", e.currentTarget.value)}
              helpText="One per line. E.g., amazon.in, ebay.com"
            />
          </div>
        </s-stack>
      </s-section>

      {/* 2. Pricing Strategy */}
      <s-section heading="💰 Pricing Strategy">
        <s-text tone="subdued" style={{ marginBottom: "12px", display: "block" }}>
          Define how your prices relate to competitors.
        </s-text>
        <s-stack direction="block" gap="base">
          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Discount off median competitor price</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  <strong>Formula:</strong> Suggested = median × (1 − discount). E.g., 5% means sell 5% cheaper than average.
                </div>
              </s-popover>
            </div>
            <s-text-field
              value={form.markupPct}
              onInput={(e) => setField("markupPct", e.currentTarget.value)}
              helpText="E.g., 0.02 or 2%. 0% = match average, higher = bigger discount."
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Budget tier discount</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  For Budget products, additional undercut below median. E.g., 5% = offer 5% cheaper than average.
                </div>
              </s-popover>
            </div>
            <s-text-field
              value={form.budgetUndercut}
              onInput={(e) => setField("budgetUndercut", e.currentTarget.value)}
              helpText="E.g., 0.05 or 5%"
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Premium tier markup</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  For Premium products, additional markup above median. E.g., 5% = charge 5% more than average.
                </div>
              </s-popover>
            </div>
            <s-text-field
              value={form.premiumUplift}
              onInput={(e) => setField("premiumUplift", e.currentTarget.value)}
              helpText="E.g., 0.05 or 5%"
            />
          </div>
        </s-stack>
      </s-section>

      {/* 3. Price Change Rules */}
      <s-section heading="🛡️ Price Change Rules">
        <s-text tone="subdued" style={{ marginBottom: "12px", display: "block" }}>
          Safety limits on automatic price updates.
        </s-text>
        <s-stack direction="block" gap="base">
          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Matched competitors needed before pricing</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  Don't update price until at least this many competitors match your product. "Matched" = semantically similar.
                </div>
              </s-popover>
            </div>
            <s-text-field
              type="number"
              value={form.minCompetitorsToPrice}
              onInput={(e) => setField("minCompetitorsToPrice", e.currentTarget.value)}
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Use most similar N competitors</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  Weight only K most-similar competitors in pricing. Avoids noise from distant matches. Top 4 = focus on true competitors.
                </div>
              </s-popover>
            </div>
            <s-text-field
              type="number"
              value={form.topKCompetitors}
              onInput={(e) => setField("topKCompetitors", e.currentTarget.value)}
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Max price change per update</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  Hard limit on price movement in one cycle. Prevents sudden big jumps. E.g., 5% = won't jump more than ±5%.
                </div>
              </s-popover>
            </div>
            <s-text-field
              value={form.maxAutoApplyChangePct}
              onInput={(e) => setField("maxAutoApplyChangePct", e.currentTarget.value)}
              helpText="E.g., 0.05 or 5%"
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Don't drift more than (lifetime)</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "16px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  Price can't stray this far from base price. E.g., $100 base with 25% cap = stay between $75–$125.
                </div>
              </s-popover>
            </div>
            <s-text-field
              value={form.lifetimeCapPct}
              onInput={(e) => setField("lifetimeCapPct", e.currentTarget.value)}
              helpText="E.g., 0.25 or 25%"
            />
          </div>
        </s-stack>
      </s-section>

      {/* 4. Update Frequency */}
      <s-section heading="⏱️ Update Frequency">
        <s-text tone="subdued" style={{ marginBottom: "12px", display: "block" }}>
          How often to check for competitor price changes.
        </s-text>
        <s-stack direction="inline" gap="base">
          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Every</label>
            </div>
            <s-text-field
              type="number"
              value={form.frequencyInterval}
              onInput={(e) => setField("frequencyInterval", e.currentTarget.value)}
            />
          </div>
          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Unit</label>
              <s-popover preferredPosition="above">
                <button aria-label="More info" slot="button" style={{ padding: "4px", background: "none", border: "none", cursor: "help", color: "#0066cc", fontSize: "14px" }}>ⓘ</button>
                <div style={{ padding: "12px", fontSize: "12px", maxWidth: "250px", lineHeight: "1.4" }}>
                  More frequent = better accuracy but higher cost. "Never" = one-time discovery only.
                </div>
              </s-popover>
            </div>
            <s-select
              value={form.frequencyUnit}
              onChange={(e) => setField("frequencyUnit", e.currentTarget.value)}
            >
              {FREQ_UNITS.map((u) => (
                <s-option key={u.value} value={u.value}>{u.label}</s-option>
              ))}
            </s-select>
          </div>
        </s-stack>
      </s-section>

      {/* 5. Controls */}
      <s-section heading="⚙️ Controls">
        <s-text tone="subdued" style={{ marginBottom: "12px", display: "block" }}>
          Master switches for the system.
        </s-text>
        <s-stack direction="block" gap="base">
          <div style={{ padding: "12px", background: "#fafafa", borderRadius: "4px", borderLeft: "3px solid #f0f0f0" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "12px", marginBottom: "8px" }}>
              <s-toggle
                checked={form.autoRescrapeEnabled || undefined}
                onChange={(e) => setField("autoRescrapeEnabled", e.currentTarget.checked)}
              />
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "4px" }}>
                  <span style={{ fontWeight: "600", margin: 0 }}>Auto rescrape</span>
                  <s-popover preferredPosition="above">
                    <button slot="button" aria-label="More info" style={{ padding: "2px 6px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "4px", cursor: "help", color: "#0066cc", fontSize: "12px", fontWeight: "bold", width: "20px", height: "20px", display: "flex", alignItems: "center", justifyContent: "center" }}>ⓘ</button>
                    <div style={{ padding: "10px", fontSize: "12px", maxWidth: "240px", lineHeight: "1.5" }}>
                      Master on/off for all competitor checks. Turn OFF to pause (e.g., testing or emergency).
                    </div>
                  </s-popover>
                </div>
                <s-text tone="subdued" style={{ fontSize: "12px" }}>Master switch for refreshing competitor prices.</s-text>
              </div>
            </div>
          </div>

          <div style={{ padding: "12px", background: "#fafafa", borderRadius: "4px", borderLeft: "3px solid #f0f0f0" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "12px", marginBottom: "8px" }}>
              <s-toggle
                checked={form.includeOosInPricing || undefined}
                onChange={(e) => setField("includeOosInPricing", e.currentTarget.checked)}
              />
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "4px" }}>
                  <span style={{ fontWeight: "600" }}>Include out-of-stock</span>
                  <s-popover preferredPosition="above">
                    <button slot="button" aria-label="More info" style={{ padding: "2px 6px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "4px", cursor: "help", color: "#0066cc", fontSize: "12px", fontWeight: "bold", width: "20px", height: "20px", display: "flex", alignItems: "center", justifyContent: "center" }}>ⓘ</button>
                    <div style={{ padding: "10px", fontSize: "12px", maxWidth: "240px", lineHeight: "1.5" }}>
                      When ON, out-of-stock competitor prices count in calculations. Turn ON if stock detection is unreliable.
                    </div>
                  </s-popover>
                </div>
                <s-text tone="subdued" style={{ fontSize: "12px" }}>Include OOS competitor prices in pricing.</s-text>
              </div>
            </div>
          </div>
        </s-stack>
      </s-section>

      <s-stack direction="inline" gap="base" align="center">
        <s-button variant="primary" onClick={submit}>Save settings</s-button>
        {saved && <s-text tone="success">Saved.</s-text>}
      </s-stack>
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
