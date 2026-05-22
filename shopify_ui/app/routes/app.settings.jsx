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
