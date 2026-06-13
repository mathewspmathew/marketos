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
  minCompetitorsRequired: 2,
  maxCompetitorsPerProduct: 8,
  frequencyInterval: 1,
  frequencyUnit: "day",
  listingExpansionCap: 5,
  marketplaceBlocklist: [],
  killSwitch: false,
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
      minCompetitorsRequired:   s.minCompetitorsRequired,
      maxCompetitorsPerProduct: s.maxCompetitorsPerProduct,
      frequencyInterval:        s.frequencyInterval,
      frequencyUnit:            s.frequencyUnit,
      listingExpansionCap:      s.listingExpansionCap ?? DEFAULTS.listingExpansionCap,
      marketplaceBlocklist:     s.marketplaceBlocklist ?? [],
      killSwitch:               s.killSwitch,
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
    minCompetitorsRequired:   parsePositiveInt(formData.get("minCompetitorsRequired"),   DEFAULTS.minCompetitorsRequired),
    maxCompetitorsPerProduct: parsePositiveInt(formData.get("maxCompetitorsPerProduct"), DEFAULTS.maxCompetitorsPerProduct),
    frequencyInterval:        parsePositiveInt(formData.get("frequencyInterval"),        DEFAULTS.frequencyInterval),
    frequencyUnit:            unit,
    listingExpansionCap:      parsePositiveInt(formData.get("listingExpansionCap"),      DEFAULTS.listingExpansionCap),
    marketplaceBlocklist:     { set: blocklist },
    killSwitch:               formData.get("killSwitch") === "true",
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

export default function SettingsPage() {
  const { settings } = useLoaderData();
  const fetcher = useFetcher();
  const saved = fetcher.data?.ok && fetcher.state === "idle";

  const [form, setForm] = useState({
    markupPct: String(settings.markupPct),
    minCompetitorsRequired: String(settings.minCompetitorsRequired),
    maxCompetitorsPerProduct: String(settings.maxCompetitorsPerProduct),
    frequencyInterval: String(settings.frequencyInterval),
    frequencyUnit: settings.frequencyUnit,
    listingExpansionCap: String(settings.listingExpansionCap),
    marketplaceBlocklist: (settings.marketplaceBlocklist ?? []).join("\n"),
    killSwitch: settings.killSwitch,
    autoRescrapeEnabled: settings.autoRescrapeEnabled,
    includeOosInPricing: settings.includeOosInPricing,
    minCompetitorsToPrice: String(settings.minCompetitorsToPrice),
    topKCompetitors:       String(settings.topKCompetitors),
    maxAutoApplyChangePct: String(settings.maxAutoApplyChangePct),
    lifetimeCapPct:        String(settings.lifetimeCapPct),
    budgetUndercut:        String(settings.budgetUndercut),
    premiumUplift:         String(settings.premiumUplift),
    serperGl:       settings.serperGl,
    serperHl:       settings.serperHl,
    serperLocation: settings.serperLocation,
  });

  const setField = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));

  const submit = () => {
    fetcher.submit(
      {
        markupPct:                form.markupPct,
        minCompetitorsRequired:   form.minCompetitorsRequired,
        maxCompetitorsPerProduct: form.maxCompetitorsPerProduct,
        frequencyInterval:        form.frequencyInterval,
        frequencyUnit:            form.frequencyUnit,
        listingExpansionCap:      form.listingExpansionCap,
        marketplaceBlocklist:     form.marketplaceBlocklist,
        killSwitch:               String(form.killSwitch),
        autoRescrapeEnabled:      String(form.autoRescrapeEnabled),
        includeOosInPricing:      String(form.includeOosInPricing),
        minCompetitorsToPrice:    form.minCompetitorsToPrice,
        topKCompetitors:          form.topKCompetitors,
        maxAutoApplyChangePct:    form.maxAutoApplyChangePct,
        lifetimeCapPct:           form.lifetimeCapPct,
        budgetUndercut:           form.budgetUndercut,
        premiumUplift:            form.premiumUplift,
        serperGl:                 form.serperGl,
        serperHl:                 form.serperHl,
        serperLocation:           form.serperLocation,
      },
      { method: "POST" },
    );
  };

  return (
    <s-page heading="Shop settings">
      <s-section heading="Pricing formula">
        <s-stack direction="block" gap="base">
          <s-text-field
            label="Markup below median (e.g. 0.02 or 2%)"
            value={form.markupPct}
            onInput={(e) => setField("markupPct", e.currentTarget.value)}
            helpText="Suggested price = median(competitors) × (1 − markup). Higher means more discount."
          />
          <s-text-field
            label="Minimum competitors required"
            type="number"
            value={form.minCompetitorsRequired}
            onInput={(e) => setField("minCompetitorsRequired", e.currentTarget.value)}
            helpText="Don't suggest a price until at least this many competitor observations exist."
          />
        </s-stack>
      </s-section>

      <s-section heading="Discovery">
        <s-stack direction="block" gap="base">
          <s-text-field
            label="Max competitors to track per product"
            type="number"
            value={form.maxCompetitorsPerProduct}
            onInput={(e) => setField("maxCompetitorsPerProduct", e.currentTarget.value)}
          />
          <s-text-field
            label="Default max products from a listing page"
            type="number"
            value={form.listingExpansionCap}
            onInput={(e) => setField("listingExpansionCap", e.currentTarget.value)}
            helpText="When a discovered URL is a search/category page, expand this many product cards out of it. Per-product and per-discovery overrides win over this."
          />
          <s-textarea
            label="Marketplace blocklist (one per line)"
            rows={5}
            value={form.marketplaceBlocklist}
            onInput={(e) => setField("marketplaceBlocklist", e.currentTarget.value)}
            helpText="Domains to exclude from competitor discovery (e.g. amazon.in, ebay.com)."
          />
        </s-stack>
      </s-section>

      <s-section heading="Search targeting (Serper)">
        <s-text tone="subdued">
          Localizes competitor discovery results. Location takes precedence over country.
        </s-text>
        <s-stack direction="block" gap="base">
          <s-text-field
            label="Location (free text, most precise)"
            value={form.serperLocation}
            onInput={(e) => setField("serperLocation", e.currentTarget.value)}
            helpText='E.g. "Kochi, Kerala" or "Mumbai, Maharashtra, India".'
          />
          <s-stack direction="inline" gap="base">
            <s-text-field
              label="Country code (gl)"
              value={form.serperGl}
              onInput={(e) => setField("serperGl", e.currentTarget.value)}
              helpText="2-letter country, e.g. in, us, gb, ae."
            />
            <s-text-field
              label="Language code (hl)"
              value={form.serperHl}
              onInput={(e) => setField("serperHl", e.currentTarget.value)}
              helpText="Language, e.g. en, hi, ar, de."
            />
          </s-stack>
        </s-stack>
      </s-section>

      <s-section heading="Auto-pricing">
        <s-text tone="subdued">
          Controls how the system moves a product's price in response to fresh
          competitor observations. Each product can override the per-round
          and lifetime caps independently.
        </s-text>
        <s-stack direction="block" gap="base">
          <s-stack direction="inline" gap="base">
            <s-text-field
              label="Minimum competitors to price"
              type="number"
              value={form.minCompetitorsToPrice}
              onInput={(e) => setField("minCompetitorsToPrice", e.currentTarget.value)}
              helpText="No price change runs until this many MATCHED competitor products have fresh observations."
            />
            <s-text-field
              label="Top-K competitors to weight"
              type="number"
              value={form.topKCompetitors}
              onInput={(e) => setField("topKCompetitors", e.currentTarget.value)}
              helpText="Only the K most-similar competitors influence the reference price (weighted by similarity)."
            />
          </s-stack>
          <s-stack direction="inline" gap="base">
            <s-text-field
              label="Max change per round (e.g. 0.05 or 5%)"
              value={form.maxAutoApplyChangePct}
              onInput={(e) => setField("maxAutoApplyChangePct", e.currentTarget.value)}
              helpText="Hard cap on |new − current| / current in one auto-apply cycle."
            />
            <s-text-field
              label="Lifetime cap from base price (e.g. 0.25 or 25%)"
              value={form.lifetimeCapPct}
              onInput={(e) => setField("lifetimeCapPct", e.currentTarget.value)}
              helpText="Price can never drift more than this from the base price snapshot, unless explicit min/max are set."
            />
          </s-stack>
          <s-stack direction="inline" gap="base">
            <s-text-field
              label="Budget tier undercut"
              value={form.budgetUndercut}
              onInput={(e) => setField("budgetUndercut", e.currentTarget.value)}
              helpText="Budget-tier products target ref × (1 − this)."
            />
            <s-text-field
              label="Premium tier uplift"
              value={form.premiumUplift}
              onInput={(e) => setField("premiumUplift", e.currentTarget.value)}
              helpText="Premium-tier products target ref × (1 + this)."
            />
          </s-stack>
        </s-stack>
      </s-section>

      <s-section heading="Default rescrape frequency">
        <s-text tone="subdued">
          Used when a product doesn't have its own frequency set on the home page.
        </s-text>
        <s-stack direction="inline" gap="base">
          <s-text-field
            label="Every"
            type="number"
            value={form.frequencyInterval}
            onInput={(e) => setField("frequencyInterval", e.currentTarget.value)}
          />
          <s-select
            label="Unit"
            value={form.frequencyUnit}
            onChange={(e) => setField("frequencyUnit", e.currentTarget.value)}
          >
            {FREQ_UNITS.map((u) => (
              <s-option key={u.value} value={u.value}>{u.label}</s-option>
            ))}
          </s-select>
        </s-stack>
      </s-section>

      <s-section heading="Safety">
        <s-stack direction="block" gap="base">
          <s-stack direction="inline" gap="base" align="center">
            <s-toggle
              checked={form.autoRescrapeEnabled || undefined}
              onClick={() => setField("autoRescrapeEnabled", !form.autoRescrapeEnabled)}
            />
            <s-text emphasis="bold">Auto rescrape</s-text>
            <s-text tone="subdued">
              Master switch for refreshing competitor prices. When off, no
              ProductUrl is rescraped — per-product frequency is preserved.
            </s-text>
          </s-stack>
          <s-stack direction="inline" gap="base" align="center">
            <s-toggle
              checked={form.includeOosInPricing || undefined}
              onClick={() => setField("includeOosInPricing", !form.includeOosInPricing)}
            />
            <s-text emphasis="bold">Include out-of-stock competitors</s-text>
            <s-text tone="subdued">
              When off, observations marked OOS are dropped from the pricing
              reference. Turn on if your scraper's stock signal is unreliable.
            </s-text>
          </s-stack>
          <s-stack direction="inline" gap="base" align="center">
            <s-toggle
              checked={form.killSwitch || undefined}
              onClick={() => setField("killSwitch", !form.killSwitch)}
            />
            <s-text emphasis="bold">Kill switch</s-text>
            <s-text tone="subdued">When on, no new PriceDecisions are written.</s-text>
          </s-stack>
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
