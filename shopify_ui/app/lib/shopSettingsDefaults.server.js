// Shared ShopSettings defaults — used both by app.settings.jsx's
// find-or-create loader and shopify.server.js's afterAuth install hook, so
// a shop always has a real ShopSettings row (with these values) from the
// moment it installs, not only after someone opens the Settings page.
export const DEFAULTS = {
  markupPct: 0.02, // discount applied to COMPETITIVE-tier products
  frequencyInterval: 1, // "every N" — paired with frequencyUnit for the default rescrape cadence
  frequencyUnit: "day", // rescrape cadence unit: never | minute | hour | day
  defaultPricingTier: "COMPETITIVE", // tier a new product starts on until the merchant picks one
  listingExpansionCap: 5, // when a discovered URL is a listing page, expand up to this many products
  discoveryNumResults: 10, // competitor products fetched per discovery run
  marketplaceBlocklist: [], // domains to exclude from competitor discovery
  autoRescrapeEnabled: true, // shop-wide master switch for scheduled rescraping
  includeOosInPricing: false, // whether out-of-stock competitor observations count toward pricing
  autoUpdatePriceEnabled: true, // shop-wide master switch: push calculated prices to Shopify automatically
  // Auto-pricing knobs (per-product overrides live on ShopifyProduct).
  minCompetitorsToPrice: 4, // minimum matched competitor products required before auto-pricing runs
  topKCompetitors: 4, // only the N highest-confidence competitor matches feed the reference price
  maxAutoApplyChangePct: 0.05, // per-round cap: max price change applied in a single pricing cycle
  lifetimeCapPct: 0.25, // lifetime cap: price can never drift more than this from basePrice (sets min/max)
  budgetUndercut: 0.05, // BUDGET tier: undercut the reference price by this fraction
  premiumUplift: 0.05, // PREMIUM tier: mark up the reference price by this fraction
  minChangePctThreshold: 0.005, // skip applying a price change smaller than this fraction (no-op guard)
  minFreshnessHours: 24, // ignore competitor observations older than this when computing a price
  serperGl: "in", // Serper (Google Search API) country code for discovery searches
  serperHl: "en", // Serper language code for discovery searches
  serperLocation: "Kochi, Kerala", // Serper geolocation bias for discovery searches
  currency: "INR", // shop's pricing currency, used for display and cross-currency observation filtering
};
