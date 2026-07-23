/**
 * app.settings.jsx — shop-wide dynamic-pricing settings form (search/
 * discovery params, pricing tiers, safety limits, frequency, master
 * switches). parsePctish's percent-vs-fraction normalization is the sole
 * writer of these fields — nothing else in the codebase writes them, so
 * this stays frontend-only input parsing, not duplicated business logic.
 */
import { useState, useEffect } from "react";
import { useFetcher, useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import db from "../db.server";
import { authenticate } from "../shopify.server";
import { DEFAULTS } from "../lib/shopSettingsDefaults.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";

// Keep in sync with services/common/frequency.py::CANONICAL_UNITS.
const FREQ_UNITS = [
  { value: "never",  label: "Never (one-time discovery)" },
  { value: "minute", label: "Minutes" },
  { value: "hour",   label: "Hours"   },
  { value: "day",    label: "Days"    },
];
const ALLOWED_UNITS = new Set(FREQ_UNITS.map((u) => u.value));

// Keep in sync with services/common/pane_config.py::PRICING_TIERS.
const PRICING_TIERS = [
  { value: "BUDGET",      label: "Budget"      },
  { value: "COMPETITIVE", label: "Competitive" },
  { value: "PREMIUM",     label: "Premium"     },
];
const ALLOWED_TIERS = new Set(PRICING_TIERS.map((t) => t.value));

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
    data: { shopDomain, ...DEFAULTS, updatedAt: new Date() },
  }));

  return {
    settings: {
      markupPct:                Number(s.markupPct), // "Discount applied to competitive tier products"
      frequencyInterval:        s.frequencyInterval, // "Every" N (with frequencyUnit)
      frequencyUnit:            s.frequencyUnit, // "Unit" (minute, hour, day, never)
      defaultPricingTier:       s.defaultPricingTier, // "Default pricing tier for new products"
      listingExpansionCap:      s.listingExpansionCap, // "Products per listing page"
      discoveryNumResults:      s.discoveryNumResults, // "Competitor products per run"
      marketplaceBlocklist:     s.marketplaceBlocklist, // "Exclude these marketplaces"
      autoRescrapeEnabled:      s.autoRescrapeEnabled, // "Auto rescrape" (master switch)
      includeOosInPricing:      s.includeOosInPricing, // "Include out-of-stock"
      minCompetitorsToPrice:    s.minCompetitorsToPrice, // "Number of minimum competitors to calculate price"
      topKCompetitors:          s.topKCompetitors, // "Focus on top N most-similar"
      maxAutoApplyChangePct:    Number(s.maxAutoApplyChangePct), // "Max price change per update"
      lifetimeCapPct:           Number(s.lifetimeCapPct), // "Don't drift more than (lifetime) - affects min and max prices"
      budgetUndercut:           Number(s.budgetUndercut), // "Discount applied to budget tier products"
      premiumUplift:            Number(s.premiumUplift), // "Markup applied to premium tier products"
      minChangePctThreshold:    Number(s.minChangePctThreshold), // "Minimum price change to apply"
      minFreshnessHours:        s.minFreshnessHours, // "Drop observations older than this"
      serperGl:                 s.serperGl, // "Country code"
      serperHl:                 s.serperHl, // "Language"
      serperLocation:           s.serperLocation, // "Search location"
      currency:                 s.currency ?? DEFAULTS.currency, // "Shop currency code"
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

  const tierRaw = (formData.get("defaultPricingTier") || "").toString();
  const tier    = ALLOWED_TIERS.has(tierRaw) ? tierRaw : DEFAULTS.defaultPricingTier;

  const blocklistRaw = (formData.get("marketplaceBlocklist") || "").toString();
  const blocklist = blocklistRaw
    .split(/[\n,]+/)
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);

  const data = {
    markupPct: parsePctish(formData.get("markupPct")) ?? DEFAULTS.markupPct,
    frequencyInterval:        parsePositiveInt(formData.get("frequencyInterval"),        DEFAULTS.frequencyInterval),
    frequencyUnit:            unit,
    defaultPricingTier:       tier,
    listingExpansionCap:      parsePositiveInt(formData.get("listingExpansionCap"),      DEFAULTS.listingExpansionCap),
    discoveryNumResults:      parsePositiveInt(formData.get("discoveryNumResults"),      DEFAULTS.discoveryNumResults),
    marketplaceBlocklist:     { set: blocklist },
    autoRescrapeEnabled:      formData.get("autoRescrapeEnabled") === "true",
    includeOosInPricing:      formData.get("includeOosInPricing") === "true",
    minCompetitorsToPrice:    parsePositiveInt(formData.get("minCompetitorsToPrice"), DEFAULTS.minCompetitorsToPrice),
    topKCompetitors:          parsePositiveInt(formData.get("topKCompetitors"),       DEFAULTS.topKCompetitors),
    maxAutoApplyChangePct:    parsePctish(formData.get("maxAutoApplyChangePct")) ?? DEFAULTS.maxAutoApplyChangePct,
    lifetimeCapPct:           parsePctish(formData.get("lifetimeCapPct"))        ?? DEFAULTS.lifetimeCapPct,
    budgetUndercut:           parsePctish(formData.get("budgetUndercut"))        ?? DEFAULTS.budgetUndercut,
    premiumUplift:            parsePctish(formData.get("premiumUplift"))         ?? DEFAULTS.premiumUplift,
    minChangePctThreshold:    parsePctish(formData.get("minChangePctThreshold")) ?? DEFAULTS.minChangePctThreshold,
    minFreshnessHours:        parsePositiveInt(formData.get("minFreshnessHours"), DEFAULTS.minFreshnessHours),
    serperGl:       ((formData.get("serperGl")       || "").toString().trim().toLowerCase()) || DEFAULTS.serperGl,
    serperHl:       ((formData.get("serperHl")       || "").toString().trim().toLowerCase()) || DEFAULTS.serperHl,
    serperLocation: ((formData.get("serperLocation") || "").toString().trim())               || DEFAULTS.serperLocation,
    currency:       ((formData.get("currency")       || "").toString().trim().toUpperCase()) || DEFAULTS.currency,
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
    create: { shopDomain, ...data, marketplaceBlocklist: blocklist, updatedAt: new Date() },
  });

  if (autoRescrapeTurnedOn) {
    // Resume the rescrape loop for every product that's still opted in with
    // a real frequency. Beat will pick them up on the next tick. The
    // re-arm calculation lives in Python (services/common/frequency.py),
    // not reimplemented here — same shop-wide re-arm rule pane_config.py
    // already applies per-product.
    const params = new URLSearchParams({
      shop_domain: shopDomain,
      frequency_interval: String(data.frequencyInterval),
      frequency_unit: data.frequencyUnit,
    });
    await fetch(`${PYTHON_API_URL}/internal/dynamic-pricing/rearm-shop?${params}`, {
      method: "POST",
    }).catch(() => {});
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

  // Convert decimal percentages (0.05) to user-friendly percentage (5) for display
  const toPercentageDisplay = (decimal) => {
    if (decimal == null) return "";
    const num = Number(decimal);
    return num > 1 ? String(num) : String(num * 100);
  };

  const initialFormState = {
    serperLocation: settings.serperLocation,
    serperGl: settings.serperGl,
    serperHl: settings.serperHl,
    listingExpansionCap: String(settings.listingExpansionCap),
    discoveryNumResults: String(settings.discoveryNumResults),
    marketplaceBlocklist: settings.marketplaceBlocklist ?? [],
    markupPct: toPercentageDisplay(settings.markupPct),
    budgetUndercut: toPercentageDisplay(settings.budgetUndercut),
    premiumUplift: toPercentageDisplay(settings.premiumUplift),
    minCompetitorsToPrice: String(settings.minCompetitorsToPrice),
    topKCompetitors: String(settings.topKCompetitors),
    maxAutoApplyChangePct: toPercentageDisplay(settings.maxAutoApplyChangePct),
    lifetimeCapPct: toPercentageDisplay(settings.lifetimeCapPct),
    minChangePctThreshold: toPercentageDisplay(settings.minChangePctThreshold),
    minFreshnessHours: String(settings.minFreshnessHours),
    frequencyInterval: String(settings.frequencyInterval),
    frequencyUnit: settings.frequencyUnit,
    defaultPricingTier: settings.defaultPricingTier,
    autoRescrapeEnabled: settings.autoRescrapeEnabled,
    includeOosInPricing: settings.includeOosInPricing,
    currency: settings.currency,
  };

  const [form, setForm] = useState(initialFormState);
  const [showSavedMessage, setShowSavedMessage] = useState(false);
  const [newBlockedDomain, setNewBlockedDomain] = useState("");

  const addBlockedDomain = () => {
    const domain = newBlockedDomain.trim().toLowerCase();
    if (!domain || form.marketplaceBlocklist.includes(domain)) return;
    setForm((prev) => ({ ...prev, marketplaceBlocklist: [...prev.marketplaceBlocklist, domain] }));
    setNewBlockedDomain("");
  };

  const removeBlockedDomain = (domain) => {
    setForm((prev) => ({
      ...prev,
      marketplaceBlocklist: prev.marketplaceBlocklist.filter((d) => d !== domain),
    }));
  };

  // Check if form has unsaved changes
  const isDirty = JSON.stringify(form) !== JSON.stringify(initialFormState);

  // Reset form to initialFormState after successful save and auto-dismiss "Saved" message
  useEffect(() => {
    if (fetcher.data?.ok && fetcher.state === "idle") {
      setForm(initialFormState);
      setShowSavedMessage(true);
      const timer = setTimeout(() => setShowSavedMessage(false), 3000);
      return () => clearTimeout(timer);
    }
  }, [fetcher.data?.ok, fetcher.state, initialFormState]);

  const setField = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));

  const submit = () => {
    fetcher.submit(
      {
        markupPct: form.markupPct,
        frequencyInterval: form.frequencyInterval,
        frequencyUnit: form.frequencyUnit,
        defaultPricingTier: form.defaultPricingTier,
        listingExpansionCap: form.listingExpansionCap,
        discoveryNumResults: form.discoveryNumResults,
        marketplaceBlocklist: form.marketplaceBlocklist.join("\n"),
        autoRescrapeEnabled: String(form.autoRescrapeEnabled),
        includeOosInPricing: String(form.includeOosInPricing),
        minCompetitorsToPrice: form.minCompetitorsToPrice,
        topKCompetitors: form.topKCompetitors,
        maxAutoApplyChangePct: form.maxAutoApplyChangePct,
        lifetimeCapPct: form.lifetimeCapPct,
        budgetUndercut: form.budgetUndercut,
        premiumUplift: form.premiumUplift,
        minChangePctThreshold: form.minChangePctThreshold,
        minFreshnessHours: form.minFreshnessHours,
        serperGl: form.serperGl,
        serperHl: form.serperHl,
        serperLocation: form.serperLocation,
        currency: form.currency,
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
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Search location</label>
              <span title="Your market location for competitor search. More precise = more relevant results. E.g., Kochi, Kerala vs. India." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <s-text-field
              value={form.serperLocation}
              onInput={(e) => setField("serperLocation", e.currentTarget.value)}
              helpText="Most precise: city, state, country. E.g., Kochi, Kerala or Mumbai, India."
            />
          </div>

          <div style={{ display: "flex", gap: "12px" }}>
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
                <label style={{ fontWeight: "500", fontSize: "14px" }}>Country code</label>
                <span title="2-letter country code (e.g., in, us, gb, ae). Falls back to this if location is vague." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
              </div>
              <s-text-field
                value={form.serperGl}
                onInput={(e) => setField("serperGl", e.currentTarget.value)}
                helpText="E.g., in, us, gb, ae"
              />
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
                <label style={{ fontWeight: "500", fontSize: "14px" }}>Language</label>
                <span title="2-letter language code (e.g., en, hi, ar, de). Filters results by language." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
              </div>
              <s-text-field
                value={form.serperHl}
                onInput={(e) => setField("serperHl", e.currentTarget.value)}
                helpText="E.g., en, hi, ar, de"
              />
            </div>
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Currency code</label>
              <span title="3-letter currency code (e.g., USD, INR, GBP, EUR). Used to display prices with the correct symbol." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <s-text-field
              value={form.currency}
              onInput={(e) => setField("currency", e.currentTarget.value.toUpperCase())}
              helpText="E.g., USD ($), INR (₹), GBP (£), EUR (€)"
              placeholder="INR"
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Products per listing page</label>
              <span title="When we find a category page, extract this many product cards. Higher = broader but slower." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <s-text-field
              type="number"
              value={form.listingExpansionCap}
              onInput={(e) => setField("listingExpansionCap", e.currentTarget.value)}
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Competitor products per run</label>
              <span title="Number of competitor search results to fetch per discovery run. Higher = more candidates to work with." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <s-text-field
              type="number"
              value={form.discoveryNumResults}
              onInput={(e) => setField("discoveryNumResults", e.currentTarget.value)}
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Exclude these marketplaces</label>
              <span title="Domains to exclude from competitor discovery. Nothing is blocked by default — add a domain (e.g. amazon.in, ebay.com) to stop scraping it." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <div style={{ display: "flex", gap: "8px", marginBottom: "8px" }}>
              <s-text-field
                value={newBlockedDomain}
                onInput={(e) => setNewBlockedDomain(e.currentTarget.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addBlockedDomain(); } }}
                placeholder="e.g. amazon.in"
                style={{ flex: 1 }}
              />
              <s-button onClick={addBlockedDomain}>Add</s-button>
            </div>
            {form.marketplaceBlocklist.length === 0 ? (
              <s-text tone="subdued" style={{ fontSize: "0.85em" }}>No domains excluded — all marketplaces are eligible for discovery.</s-text>
            ) : (
              <s-stack direction="block" gap="tight">
                {form.marketplaceBlocklist.map((domain) => (
                  <div key={domain} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "6px 10px", background: "#f6f6f7", borderRadius: "6px" }}>
                    <span>{domain}</span>
                    <s-button kind="tertiary" size="small" onClick={() => removeBlockedDomain(domain)}>Remove</s-button>
                  </div>
                ))}
              </s-stack>
            )}
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
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Discount applied to competitive tier products </label>
              <span title="Suggested price = median × (1 - discount). E.g., 5% means sell 5% cheaper than average." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <s-text-field
                type="number"
                value={form.markupPct}
                onInput={(e) => setField("markupPct", e.currentTarget.value)}
                placeholder="5"
                style={{ flex: 1 }}
              />
              <span style={{ fontSize: "14px", color: "#666", fontWeight: "500" }}>%</span>
            </div>
            <s-text tone="subdued" style={{ fontSize: "0.85em", marginTop: "4px", display: "block" }}>E.g., enter 5 or 15</s-text>
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Discount applied to budget tier products</label>
              <span title="For Budget products, additional undercut below median. E.g., 5% = offer 5% cheaper than average." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <s-text-field
                type="number"
                value={form.budgetUndercut}
                onInput={(e) => setField("budgetUndercut", e.currentTarget.value)}
                placeholder="5"
                style={{ flex: 1 }}
              />
              <span style={{ fontSize: "14px", color: "#666", fontWeight: "500" }}>%</span>
            </div>
            <s-text tone="subdued" style={{ fontSize: "0.85em", marginTop: "4px", display: "block" }}>E.g., enter 5 or 15</s-text>
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Markup applied to premium tier products</label>
              <span title="For Premium products, additional markup above median. E.g., 5% = charge 5% more than average." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <s-text-field
                type="number"
                value={form.premiumUplift}
                onInput={(e) => setField("premiumUplift", e.currentTarget.value)}
                placeholder="5"
                style={{ flex: 1 }}
              />
              <span style={{ fontSize: "14px", color: "#666", fontWeight: "500" }}>%</span>
            </div>
            <s-text tone="subdued" style={{ fontSize: "0.85em", marginTop: "4px", display: "block" }}>E.g., enter 5 or 15</s-text>
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
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Number of minimum competitors to calculate price</label>
              <span title="Safety gate: don't calculate price until we have at least this many matched (semantically similar) competitor products. Higher = more confident but slower." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <s-text-field
              type="number"
              value={form.minCompetitorsToPrice}
              onInput={(e) => setField("minCompetitorsToPrice", e.currentTarget.value)}
              helpText="E.g., 4. Ensures enough signal before pricing activates."
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Focus on top N most-similar</label>
              <span title="Quality filter: among all matched competitors, use only the K most-confident for price calculation. Avoids noise from distant matches. E.g., top 3 = use the 3 strongest matches." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <s-text-field
              type="number"
              value={form.topKCompetitors}
              onInput={(e) => setField("topKCompetitors", e.currentTarget.value)}
              helpText="E.g., 3 or 4. Used for weighted average calculation."
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Max price change per update</label>
              <span title="Hard limit on price movement in one cycle. Prevents sudden big jumps. E.g., 5% = won't jump more than ±5%." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <s-text-field
                type="number"
                value={form.maxAutoApplyChangePct}
                onInput={(e) => setField("maxAutoApplyChangePct", e.currentTarget.value)}
                placeholder="5"
                style={{ flex: 1 }}
              />
              <span style={{ fontSize: "14px", color: "#666", fontWeight: "500" }}>%</span>
            </div>
            <s-text tone="subdued" style={{ fontSize: "0.85em", marginTop: "4px", display: "block" }}>E.g., enter 5 or 15</s-text>
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Don't drift more than (lifetime) - affects min and max prices</label>
              <span title="Price can't stray this far from base price. E.g., $100 base with 25% cap = stay between $75–$125." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <s-text-field
                type="number"
                value={form.lifetimeCapPct}
                onInput={(e) => setField("lifetimeCapPct", e.currentTarget.value)}
                placeholder="25"
                style={{ flex: 1 }}
              />
              <span style={{ fontSize: "14px", color: "#666", fontWeight: "500" }}>%</span>
            </div>
            <s-text tone="subdued" style={{ fontSize: "0.85em", marginTop: "4px", display: "block" }}>E.g., enter 5 or 25</s-text>
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Minimum price change to apply in a single run</label>
              <span title="Ignore price changes smaller than this threshold. Prevents applying tiny 0.1% wiggles. E.g., 0.5% = only apply if change is ≥0.5%. The goal is to avoid quick apply of prices when competitors matched. Don't change if you don't understand" style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <s-text-field
                type="number"
                value={form.minChangePctThreshold}
                onInput={(e) => setField("minChangePctThreshold", e.currentTarget.value)}
                placeholder="0.5"
                style={{ flex: 1 }}
              />
              <span style={{ fontSize: "14px", color: "#666", fontWeight: "500" }}>%</span>
            </div>
            <s-text tone="subdued" style={{ fontSize: "0.85em", marginTop: "4px", display: "block" }}>Default: 0.5% (ignore changes smaller than this)</s-text>
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Observation freshness requirement</label>
              <span title="Drop competitor price observations older than this many hours. Ensures pricing is based on recent data. E.g., 24 = ignore prices older than 24 hours." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <s-text-field
              type="number"
              value={form.minFreshnessHours}
              onInput={(e) => setField("minFreshnessHours", e.currentTarget.value)}
              helpText="Default: 24 hours. Prices older than this are considered stale."
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
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "500" }}>Every</label>
              <span title="Default rescrape interval. More frequent = better accuracy but higher cost. Never = one-time discovery only." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
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

      <s-section heading="🏷️ Default Pricing Tier">
        <s-text tone="subdued" style={{ marginBottom: "12px", display: "block" }}>
          Used for new products when the chatbot isn't told a tier explicitly.
        </s-text>
        <s-select
          value={form.defaultPricingTier}
          onChange={(e) => setField("defaultPricingTier", e.currentTarget.value)}
        >
          {PRICING_TIERS.map((t) => (
            <s-option key={t.value} value={t.value}>{t.label}</s-option>
          ))}
        </s-select>
      </s-section>

      {/* 5. Controls */}
      <s-section heading="⚙️ Controls">
        <s-text tone="subdued" style={{ marginBottom: "12px", display: "block" }}>
          Master switches for the system.
        </s-text>
        <s-stack direction="block" gap="base">
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "600" }}>Auto rescrape</label>
              <span title="Master on/off for all competitor checks. Turn OFF to pause (e.g., testing or emergency)." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <s-checkbox
              id="auto-rescrape"
              checked={form.autoRescrapeEnabled || undefined}
              onChange={() => setField("autoRescrapeEnabled", !form.autoRescrapeEnabled)}
              helpText="Master switch for refreshing competitor prices."
            />
          </div>

          <div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <label style={{ fontWeight: "600" }}>Include out-of-stock</label>
              <span title="When ON, OOS competitor prices count in calculations. Turn ON if stock detection is unreliable." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
            </div>
            <s-checkbox
              id="include-oos"
              checked={form.includeOosInPricing || undefined}
              onChange={() => setField("includeOosInPricing", !form.includeOosInPricing)}
              helpText="Include OOS competitor prices in pricing calculations."
            />
          </div>
        </s-stack>
      </s-section>

      <s-stack direction="inline" gap="base" alignItems="center">
        <s-button variant="primary" onClick={submit}>Save settings</s-button>
        {isDirty && <s-text tone="warning">Not saved</s-text>}
        {showSavedMessage && <s-text tone="success">Saved!</s-text>}
      </s-stack>
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
