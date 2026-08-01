/**
 * app.products.jsx — Dynamic Pricing product catalog: list, search/filter,
 * and per-product config panel (search query, discovery settings, pricing
 * tier, price bounds, rescrape frequency), plus the manual product-sync
 * trigger. calculateMinMaxPrices() is a deliberate exception to "compute
 * bounds in Python": it's a live per-keystroke preview as the merchant
 * edits the pricing tier, not the authoritative clamp — decide.py still
 * enforces the real lifetime-cap bound server-side at pricing time.
 */
import { useState, useMemo, useEffect, useRef } from "react";
import { useFetcher, useLoaderData, useRevalidator } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";
import { getCurrencySymbol } from "../lib/currencyFormatter";

// ─── Loader ──────────────────────────────────────────────────────────────────
export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  // Ensure ShopifyUser row exists for this shop. update:{} is a no-op, so
  // the returned row already reflects current state — no need to re-fetch it.
  const freshUser = await db.shopifyUser.upsert({
    where: { shopDomain },
    update: {},
    create: { shopDomain },
  });

  const [products, shopSettings, processingCount, hasOfflineSession] = await Promise.all([
    db.shopifyProduct.findMany({
      where: { shopDomain },
      include: {
        ShopifyVariant: { take: 1 },
        _count: { select: { CompetitorCandidate: true, ProductLevelMatch: true } },
      },
      orderBy: { updatedAt: "desc" },
    }),
    db.shopSettings.findUnique({ where: { shopDomain } }),
    db.shopifyProduct.count({
      where: {
        shopDomain,
        updatedAt: { gte: new Date(Date.now() - 15 * 60 * 1000) },
        ShopifyVariant: { some: { semanticText: null } },
      },
    }),
    // With expiring offline tokens enabled, individual expiry is normal —
    // the library auto-refreshes via Token Exchange on the next call. The
    // only state worth banner-ing is "no offline session row at all", which
    // means install never completed (or was wiped).
    db.session.findFirst({
      where: { shop: shopDomain, isOnline: false },
      select: { id: true },
    }).then(Boolean),
  ]);

  const flattened = products.map((p) => ({
    id: p.id,
    title: p.title,
    productType: p.productType,
    tags: p.tags,
    imageUrl: p.imageUrl,
    price: p.ShopifyVariant[0]?.currentPrice?.toString() ?? "0.00",
    compareAtPrice: p.ShopifyVariant[0]?.compareAtPrice?.toString() ?? null,
    dynamicPricingEnabled: p.dynamicPricingEnabled,
    dynamicPricingConfigured: p.dynamicPricingConfiguredAt != null,
    syncPrice: p.syncPrice,
    searchQuery: p.searchQuery ?? "",
    searchQueryOverride: p.searchQueryOverride ?? "",
    pricingTier: p.pricingTier ?? "COMPETITIVE",
    avgBasePrice: p.avgBasePrice?.toString() ?? null,
    minPriceOverride: p.minPriceOverride?.toString() ?? "",
    maxPriceOverride: p.maxPriceOverride?.toString() ?? "",
    frequencyInterval: p.frequencyInterval ?? "",
    frequencyUnit: p.frequencyUnit ?? "",
    discoveryNumResults: p.discoveryNumResults ?? "",
    listingExpansionCap: p.listingExpansionCap ?? "",
    lastDiscoveryAt: p.lastDiscoveryAt ? p.lastDiscoveryAt.toISOString() : null,
    matchCount: p._count.ProductLevelMatch,
    candidateCount: p._count.CompetitorCandidate,
  }));

  return {
    products: flattened,
    auth: { hasOfflineSession },
    productSyncState: freshUser?.productSyncState ?? "IDLE",
    productSyncedAt: freshUser?.productSyncedAt ? freshUser.productSyncedAt.toISOString() : null,
    processingCount,
    shopDefaults: shopSettings,
  };
};

// Canonical frequency-unit dropdown — keep in sync with
// services/common/frequency.py::CANONICAL_UNITS / UNIT_LABELS.
const FREQ_UNITS = [
  { value: "never",  label: "Never (one-time discovery)" },
  { value: "minute", label: "Minutes" },
  { value: "hour",   label: "Hours"   },
  { value: "day",    label: "Days"    },
];

const SECTION_HELP_TEXT_STYLE = { fontSize: "0.9em", marginBottom: "8px", display: "block" };

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_HEADERS = { "X-Internal-Token": process.env.INTERNAL_API_TOKEN };

// ─── Action ───────────────────────────────────────────────────────────────────
export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const formData = await request.formData();
  const intent = formData.get("intent");
  const productId = formData.get("productId");
  const shopDomain = session.shop;

  if (intent === "syncProducts") {
    // Python atomically claims the sync slot (or no-ops if one's already in
    // flight) and enqueues the pull — no local read/write here, so this and
    // the homepage's auto-kick can't race each other.
    void fetch(
      `${PYTHON_API_URL}/internal/shopify/sync-products?shop_domain=${encodeURIComponent(shopDomain)}`,
      { method: "POST", headers: INTERNAL_HEADERS },
    ).catch(() => {});
    return { ok: true };
  }

  if (intent === "saveAndEnable" || intent === "updateOverrides") {
    const toNullableString = (raw) => {
      const v = (raw || "").toString().trim();
      return v === "" ? null : v;
    };
    const toNullableNumber = (raw) => (raw === "" || raw == null ? null : Number(raw));

    const minRaw = formData.get("minPriceOverride");
    const maxRaw = formData.get("maxPriceOverride");
    const minEmpty = minRaw === "" || minRaw == null;
    const maxEmpty = maxRaw === "" || maxRaw == null;

    const config = {
      search_query_override: (formData.get("searchQueryOverride") || "").toString().trim(),
      pricing_tier: toNullableString(formData.get("pricingTier")),
      min_price_override: minEmpty ? null : Number(minRaw),
      max_price_override: maxEmpty ? null : Number(maxRaw),
      clear_min_price_override: minEmpty,
      clear_max_price_override: maxEmpty,
      frequency_unit: toNullableString(formData.get("frequencyUnit")),
      frequency_interval: toNullableNumber(formData.get("frequencyInterval")),
      discovery_num_results: toNullableNumber(formData.get("discoveryNumResults")),
      listing_expansion_cap: toNullableNumber(formData.get("listingExpansionCap")),
    };

    const res = await fetch(`${PYTHON_API_URL}/internal/dynamic-pricing/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...INTERNAL_HEADERS },
      body: JSON.stringify({ shop_domain: shopDomain, product_id: productId, config }),
    });
    const data = await res.json();
    return data.ok ? { ok: true } : { ok: false, error: data.error };
  }

  if (intent === "pauseDynamic") {
    const res = await fetch(`${PYTHON_API_URL}/internal/dynamic-pricing/pause`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...INTERNAL_HEADERS },
      body: JSON.stringify({ shop_domain: shopDomain, product_id: productId }),
    });
    const data = await res.json();
    return data.ok ? { ok: true } : { ok: false, error: data.error };
  }

  if (intent === "resumeDynamic") {
    const res = await fetch(`${PYTHON_API_URL}/internal/dynamic-pricing/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...INTERNAL_HEADERS },
      body: JSON.stringify({ shop_domain: shopDomain, product_id: productId }),
    });
    const data = await res.json();
    return data.ok ? { ok: true } : { ok: false, error: data.error };
  }

  if (intent === "deleteDynamicWithData") {
    const res = await fetch(`${PYTHON_API_URL}/internal/dynamic-pricing/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...INTERNAL_HEADERS },
      body: JSON.stringify({ shop_domain: shopDomain, product_id: productId, confirmed: true }),
    });
    const data = await res.json();
    return data.ok ? { ok: true } : { ok: false, error: data.error };
  }

  return null;
};

// ─── UI ───────────────────────────────────────────────────────────────────────
export default function HomePage() {
  const { products, auth, productSyncState, productSyncedAt, processingCount, shopDefaults } = useLoaderData();
  const fetcher = useFetcher();
  const revalidator = useRevalidator();
  const syncFetcher = useFetcher();
  // Every fetcher.submit below is optimistic (flips local UI state before the
  // backend confirms). This tracks the pre-submit snapshot so a {ok:false}
  // response can roll the UI back to reality instead of silently showing a
  // fake "success" state when the save was actually rejected.
  const lastActionRef = useRef(null);

  useEffect(() => {
    if (fetcher.state !== "idle" || !fetcher.data || !lastActionRef.current) return;
    const { productId, priorState } = lastActionRef.current;
    lastActionRef.current = null;
    if (!fetcher.data.ok) {
      setLocalState((prev) => ({
        ...prev,
        [productId]: { ...priorState, saveError: fetcher.data.error || "Something went wrong. Please try again." },
      }));
    } else {
      setLocalState((prev) => ({
        ...prev,
        [productId]: { ...prev[productId], saveError: null },
      }));
    }
  }, [fetcher.data, fetcher.state]);

  const isBusy = productSyncState === "SYNCING" || processingCount > 0;
  useEffect(() => {
    if (!isBusy) return;
    const t = setInterval(() => revalidator.revalidate(), 2000);
    return () => clearInterval(t);
  }, [isBusy, revalidator]);

  const [searchQuery, setSearchQuery] = useState("");
  const [selectedCategory, setSelectedCategory] = useState("all");
  const [expandedId, setExpandedId] = useState(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [openMenuId, setOpenMenuId] = useState(null);
  const [deleteConfirmId, setDeleteConfirmId] = useState(null);
  const itemsPerPage = 20;

  const [localState, setLocalState] = useState(() => {
    const map = {};
    for (const p of products) {
      map[p.id] = {
        dynamicPricingEnabled: p.dynamicPricingEnabled,
        syncPrice: p.syncPrice,
        searchQueryOverride: p.searchQueryOverride,
        // ShopifyProduct.pricingTier has a DB-level default (COMPETITIVE), so
        // it's never actually null — it can't distinguish "merchant chose
        // this" from "never configured yet". Before the first-ever configure,
        // show the shop's own default tier instead of always defaulting to
        // COMPETITIVE; after that, the product's real saved tier is authoritative.
        pricingTier: p.dynamicPricingConfigured
          ? (p.pricingTier ?? "COMPETITIVE")
          : (shopDefaults?.defaultPricingTier ?? p.pricingTier ?? "COMPETITIVE"),
        avgBasePrice: p.avgBasePrice,
        minPriceOverride: p.minPriceOverride,
        maxPriceOverride: p.maxPriceOverride,
        frequencyInterval: p.frequencyInterval === "" ? "" : String(p.frequencyInterval),
        frequencyUnit: p.frequencyUnit,
        discoveryNumResults: p.discoveryNumResults || (shopDefaults?.discoveryNumResults ?? ""),
        listingExpansionCap: p.listingExpansionCap || (shopDefaults?.listingExpansionCap ?? ""),
        price: p.price,
        calculatedMinPrice: null,
        calculatedMaxPrice: null,
      };
    }
    return map;
  });

  useEffect(() => {
    if (expandedId) {
      const product = products.find((p) => p.id === expandedId);
      const local = getLocal(expandedId);
      const priceForCalculation = local.avgBasePrice || local.price;
      if (product && priceForCalculation) {
        const lifetimeCapPct = shopDefaults?.lifetimeCapPct ?? 0.25;
        const { min, max } = calculateMinMaxPrices(Number(priceForCalculation), lifetimeCapPct);
        // Pre-fill the editable Min/Max fields with the live-computed band
        // when the merchant hasn't saved bounds yet. Saved (or typed) values
        // are never touched — Save & Enable persists whatever is in the
        // fields.
        const prefill = local.minPriceOverride === "" && local.maxPriceOverride === ""
          && min != null && max != null;
        setLocalState((prev) => ({
          ...prev,
          [expandedId]: {
            ...prev[expandedId],
            calculatedMinPrice: min,
            calculatedMaxPrice: max,
            ...(prefill
              ? { minPriceOverride: min.toFixed(2), maxPriceOverride: max.toFixed(2) }
              : {}),
          },
        }));
      }
    }
  }, [expandedId, products, shopDefaults]);

  const allCategories = useMemo(() => {
    const catSet = new Set(products.map((p) => p.productType).filter(Boolean));
    return [...catSet].sort();
  }, [products]);

  const filteredProducts = useMemo(() => {
    return products.filter((p) => {
      const matchesSearch = p.title.toLowerCase().includes(searchQuery.toLowerCase());
      const matchesCategory = selectedCategory === "all" || p.productType === selectedCategory;
      return matchesSearch && matchesCategory;
    });
  }, [products, searchQuery, selectedCategory]);

  const paginatedProducts = useMemo(() => {
    const startIndex = (currentPage - 1) * itemsPerPage;
    const endIndex = startIndex + itemsPerPage;
    return filteredProducts.slice(startIndex, endIndex);
  }, [filteredProducts, currentPage, itemsPerPage]);

  const totalPages = Math.ceil(filteredProducts.length / itemsPerPage);

  useEffect(() => {
    setCurrentPage(1);
  }, [searchQuery, selectedCategory]);

  const getLocal = (id) =>
    localState[id] ?? {
      dynamicPricingEnabled: false,
      syncPrice: true,
      searchQueryOverride: "",
      pricingTier: "COMPETITIVE",
      avgBasePrice: null,
      minPriceOverride: "",
      maxPriceOverride: "",
      frequencyInterval: "",
      frequencyUnit: "",
      discoveryNumResults: "",
      listingExpansionCap: "",
      price: "0.00",
      calculatedMinPrice: null,
      calculatedMaxPrice: null,
    };

  const getCurrentDefaults = () => shopDefaults ?? {
    frequencyInterval: "",
    frequencyUnit: "",
    listingExpansionCap: "",
    discoveryNumResults: "",
  };

  const calculateMinMaxPrices = (basePrice, lifetimeCapPct) => {
    if (!basePrice || !lifetimeCapPct || basePrice <= 0 || lifetimeCapPct <= 0) {
      return { min: null, max: null };
    }
    const minPrice = basePrice * (1 - lifetimeCapPct);
    const maxPrice = basePrice * (1 + lifetimeCapPct);
    return { min: minPrice, max: maxPrice };
  };

  const setOverrideField = (productId, field, value) => {
    setLocalState((prev) => ({
      ...prev,
      [productId]: { ...getLocal(productId), [field]: value },
    }));
  };

  const submitOverrides = (productId, opts = {}) => {
    const local = getLocal(productId);
    // Bounds sanity: both must be > 0 and min strictly below max. A max of 0
    // would make the pricing engine clamp every variant to 0.
    const minB = local.minPriceOverride === "" || local.minPriceOverride == null ? null : Number(local.minPriceOverride);
    const maxB = local.maxPriceOverride === "" || local.maxPriceOverride == null ? null : Number(local.maxPriceOverride);
    let boundsError = null;
    if (minB != null && (!Number.isFinite(minB) || minB <= 0)) {
      boundsError = "Minimum price must be greater than 0.";
    } else if (maxB != null && (!Number.isFinite(maxB) || maxB <= 0)) {
      boundsError = "Maximum price must be greater than 0.";
    } else if (minB != null && maxB != null && minB >= maxB) {
      boundsError = "Minimum price must be below the maximum.";
    }
    setOverrideField(productId, "boundsError", boundsError);
    if (boundsError) return;
    const intent = opts.enable ? "saveAndEnable" : "updateOverrides";
    lastActionRef.current = { productId, priorState: local };
    if (opts.enable) {
      // Reflect the enabled state locally so the UI flips immediately.
      setLocalState((prev) => ({
        ...prev,
        [productId]: { ...local, dynamicPricingEnabled: true },
      }));
    }
    fetcher.submit(
      {
        intent,
        productId,
        searchQueryOverride: local.searchQueryOverride ?? "",
        pricingTier:         local.pricingTier ?? "COMPETITIVE",
        // A blank bounds field only happens if the merchant cleared the
        // auto-calculated value — fall back to it rather than saving null.
        minPriceOverride:    local.minPriceOverride || (local.calculatedMinPrice != null ? local.calculatedMinPrice.toFixed(2) : ""),
        maxPriceOverride:    local.maxPriceOverride || (local.calculatedMaxPrice != null ? local.calculatedMaxPrice.toFixed(2) : ""),
        // Persist what the pane displays: the product's own frequency, else
        // the shop default the fields are showing. "never" passes through.
        frequencyInterval:   String(local.frequencyInterval || getCurrentDefaults().frequencyInterval || ""),
        frequencyUnit:       local.frequencyUnit || getCurrentDefaults().frequencyUnit || "",
        discoveryNumResults: String(local.discoveryNumResults ?? ""),
        listingExpansionCap: String(local.listingExpansionCap ?? ""),
      },
      { method: "POST" },
    );
  };

  const handlePause = (productId) => {
    lastActionRef.current = { productId, priorState: getLocal(productId) };
    setLocalState((prev) => ({
      ...prev,
      [productId]: {
        ...prev[productId],
        dynamicPricingEnabled: false,
      },
    }));
    fetcher.submit(
      { intent: "pauseDynamic", productId },
      { method: "POST" },
    );
  };

  const handleResume = (productId) => {
    lastActionRef.current = { productId, priorState: getLocal(productId) };
    setLocalState((prev) => ({
      ...prev,
      [productId]: {
        ...prev[productId],
        dynamicPricingEnabled: true,
      },
    }));
    fetcher.submit(
      { intent: "resumeDynamic", productId },
      { method: "POST" },
    );
  };

  const confirmDelete = (productId) => {
    lastActionRef.current = { productId, priorState: getLocal(productId) };
    setLocalState((prev) => ({
      ...prev,
      [productId]: {
        ...prev[productId],
        dynamicPricingEnabled: false,
        frequencyInterval: "",
        frequencyUnit: "",
        pricingTier: "COMPETITIVE",
        minPriceOverride: "",
        maxPriceOverride: "",
        searchQueryOverride: "",
        listingExpansionCap: "",
        discoveryNumResults: "",
      },
    }));
    fetcher.submit(
      { intent: "deleteDynamicWithData", productId },
      { method: "POST" },
    );
    setDeleteConfirmId(null);
  };

  const toggleExpand = (id) => {
    setExpandedId((prev) => (prev === id ? null : id));
  };

  // Auto-pricing only fails permanently when there's no offline session at
  // all (install never completed). Per-token expiry is handled by the
  // library on the next call, so we don't surface it.
  const missingOffline = auth && !auth.hasOfflineSession;

  return (
    <s-page
      heading="Dynamic Pricing"
      subheading={`${filteredProducts.length} of ${products.length} product${products.length === 1 ? "" : "s"}`}
    >
      <s-stack direction="inline" gap="base" alignment="center">
        {productSyncState === "ERROR" ? (
          <s-badge tone="critical">Sync failed</s-badge>
        ) : isBusy ? (
          <s-badge tone="info">
            {processingCount > 0 ? `Updating ${processingCount} product${processingCount === 1 ? "" : "s"}…` : "Updating…"}
          </s-badge>
        ) : (
          <s-badge tone="success">
            {productSyncedAt ? `Synced ✓ ${new Date(productSyncedAt).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true })}` : "Synced ✓"}
          </s-badge>
        )}
        <syncFetcher.Form method="post">
          <input type="hidden" name="intent" value="syncProducts" />
          <s-button variant="secondary" type="submit" {...(productSyncState === "SYNCING" ? { disabled: true } : {})}>
            {productSyncState === "ERROR" ? "Retry" : "Refresh"}
          </s-button>
        </syncFetcher.Form>
      </s-stack>

      {missingOffline && (
        <s-banner tone="critical">
          <s-text emphasis="bold">App install incomplete.</s-text>{" "}
          <s-text>No offline session for this shop — auto-pricing cannot push to Shopify. Reinstall the app from Shopify Admin to fix.</s-text>
        </s-banner>
      )}

      <s-section heading="Filters">
        <s-stack direction="inline" gap="base" wrap>
          <s-text-field
            label="Search products"
            placeholder="Search by name…"
            value={searchQuery}
            onInput={(e) => setSearchQuery(e.currentTarget.value)}
            clearButton
            onClearButtonClick={() => setSearchQuery("")}
          />
          <s-select
            label="Category"
            value={selectedCategory}
            onChange={(e) => setSelectedCategory(e.currentTarget.value)}
          >
            <s-option value="all">All Categories</s-option>
            {allCategories.map((cat) => (
              <s-option key={cat} value={cat}>{cat}</s-option>
            ))}
          </s-select>
        </s-stack>
      </s-section>

      <s-section heading="Products">
        {filteredProducts.length === 0 ? (
          <s-stack direction="block" gap="small" alignItems="center">
            <s-text emphasis="bold">No products match your filters</s-text>
            <s-text tone="subdued">
              Try clearing the search or selecting a different tag or category.
            </s-text>
          </s-stack>
        ) : (
          <>
            {/* Column header — mirrors the row grid below so labels line up with values */}
            <s-box paddingBlockEnd="tight">
              <s-grid gridTemplateColumns="50px minmax(180px,1fr) 90px 160px 150px 90px 90px" gap="base" alignItems="center">
                <div />
                <s-text tone="subdued" type="small">Product</s-text>
                <s-text tone="subdued" type="small">Price</s-text>
                <s-text tone="subdued" type="small">DP status</s-text>
                <s-text tone="subdued" type="small">Rescrape</s-text>
                <s-text tone="subdued" type="small">Matches</s-text>
                <div />
              </s-grid>
            </s-box>
            <s-divider />
            <s-stack direction="block" gap="none">
              {paginatedProducts.flatMap((product, idx) => {
              const local = getLocal(product.id);
              const isOn = local.dynamicPricingEnabled;
              const isExpanded = expandedId === product.id;
              const isPausedConfigured = !isOn && local.frequencyUnit && local.frequencyUnit !== "never";

              const row = (
                <s-box key={product.id} padding="base">
                  {/* Product Row */}
                  <s-grid gridTemplateColumns="50px minmax(180px,1fr) 90px 160px 150px 90px 90px" gap="base" alignItems="center">
                    {product.imageUrl ? (
                      <img
                        src={product.imageUrl}
                        alt={product.title}
                        width="50"
                        height="50"
                        style={{ objectFit: "cover", borderRadius: "4px" }}
                      />
                    ) : (
                      <div style={{ width: "50px", height: "50px", background: "#ddd", borderRadius: "4px" }} />
                    )}

                    <s-stack direction="block" gap="small">
                      <s-text emphasis="bold">{product.title}</s-text>
                      <s-text tone="subdued">{product.productType || "Product"}</s-text>
                    </s-stack>

                    <s-text emphasis="bold">{getCurrencySymbol(shopDefaults?.currency)}{product.price}</s-text>

                    {(isOn || isPausedConfigured) ? (
                      <s-stack direction="inline" gap="small" alignItems="center">
                        <s-badge tone={isOn ? "success" : "warning"}>{isOn ? "ON" : "Paused"}</s-badge>
                        <div style={{ position: "relative" }}>
                          <s-button
                            variant="plain"
                            size="slim"
                            onClick={() => setOpenMenuId(openMenuId === product.id ? null : product.id)}
                          >
                            ⋮
                          </s-button>
                          {openMenuId === product.id && (
                            <div style={{
                              position: "absolute",
                              top: "100%",
                              right: 0,
                              background: "white",
                              border: "1px solid #ddd",
                              borderRadius: "4px",
                              boxShadow: "0 2px 8px rgba(0,0,0,0.1)",
                              zIndex: 100,
                              minWidth: "140px",
                            }}>
                              {isOn && local.frequencyUnit && local.frequencyUnit !== "never" && (
                                <>
                                  <button
                                    onClick={() => {
                                      handlePause(product.id);
                                      setOpenMenuId(null);
                                    }}
                                    style={{
                                      display: "block",
                                      width: "100%",
                                      padding: "8px 12px",
                                      border: "none",
                                      background: "none",
                                      cursor: "pointer",
                                      fontSize: "12px",
                                      textAlign: "left",
                                    }}
                                    onMouseEnter={(e) => e.target.style.background = "#f5f5f5"}
                                    onMouseLeave={(e) => e.target.style.background = "none"}
                                  >
                                    Pause
                                  </button>
                                  <div style={{ borderTop: "1px solid #f0f0f0" }} />
                                </>
                              )}
                              {!isOn && (
                                <>
                                  <button
                                    onClick={() => {
                                      handleResume(product.id);
                                      setOpenMenuId(null);
                                    }}
                                    style={{
                                      display: "block",
                                      width: "100%",
                                      padding: "8px 12px",
                                      border: "none",
                                      background: "none",
                                      cursor: "pointer",
                                      fontSize: "12px",
                                      textAlign: "left",
                                    }}
                                    onMouseEnter={(e) => e.target.style.background = "#f5f5f5"}
                                    onMouseLeave={(e) => e.target.style.background = "none"}
                                  >
                                    Resume
                                  </button>
                                  <div style={{ borderTop: "1px solid #f0f0f0" }} />
                                </>
                              )}
                              <button
                                onClick={() => {
                                  setDeleteConfirmId(product.id);
                                  setOpenMenuId(null);
                                  document.getElementById("delete-dp-modal")?.showOverlay();
                                }}
                                style={{
                                  display: "block",
                                  width: "100%",
                                  padding: "8px 12px",
                                  border: "none",
                                  background: "none",
                                  cursor: "pointer",
                                  fontSize: "12px",
                                  textAlign: "left",
                                  color: "#d32f2f",
                                }}
                                onMouseEnter={(e) => e.target.style.background = "#ffebee"}
                                onMouseLeave={(e) => e.target.style.background = "none"}
                              >
                                Delete with Data
                              </button>
                            </div>
                          )}
                        </div>
                      </s-stack>
                    ) : (
                      <s-badge tone="subdued">OFF</s-badge>
                    )}

                    {isOn && local.frequencyUnit && local.frequencyUnit !== "never" ? (
                      <s-text tone="subdued">Rescrape every {local.frequencyInterval || ""} {local.frequencyUnit}</s-text>
                    ) : (
                      <s-text tone="subdued">–</s-text>
                    )}

                    <s-text tone="subdued">{product.matchCount || 0} match{product.matchCount === 1 ? "" : "es"}</s-text>

                    <s-button
                      variant="plain"
                      size="slim"
                      onClick={() => toggleExpand(product.id)}
                    >
                      {isExpanded ? "▾ Hide" : "▸ Details"}
                    </s-button>
                  </s-grid>

                  {/* Expanded Details Panel */}
                  {isExpanded && (
                    <div style={{ marginTop: "16px" }}>
                      <s-box
                        padding="base"
                        borderWidth="base"
                        borderRadius="base"
                        background="subdued"
                      >
                        <s-stack direction="block" gap="base">
                          {/* === SECTION 1: Search Query === */}
                          <div>
                            <s-text emphasis="bold">Search Query</s-text>
                            <div>
                              <s-text tone="subdued" style={SECTION_HELP_TEXT_STYLE}>
                                What competitors should we search for?
                              </s-text>
                            </div>
                            <s-text-field
                              label="Search query"
                              placeholder={product.searchQuery || "e.g. nike air max 90 blue mens"}
                              value={local.searchQueryOverride || product.searchQuery || ""}
                              helpText={
                                product.searchQuery && !local.searchQueryOverride
                                  ? `AI-generated from product details. Edit to refine.`
                                  : `Edit to override the generated query.`
                              }
                              onInput={(e) => {
                                const v = e.currentTarget.value;
                                setOverrideField(
                                  product.id,
                                  "searchQueryOverride",
                                  v === (product.searchQuery || "") ? "" : v,
                                );
                              }}
                            />
                          </div>

                          <s-divider />

                          {/* === SECTION 2: Discovery Settings === */}
                          <div>
                            <s-text emphasis="bold">Discovery Settings</s-text>
                            <div>
                              <s-text tone="subdued" style={SECTION_HELP_TEXT_STYLE}>
                                How many competitor products and listings should we explore?
                              </s-text>
                            </div>

                            <div style={{ marginBottom: "12px" }}>
                              <s-text-field
                                label="Competitor products per run"
                                type="number"
                                min="1"
                                max="50"
                                disabled={product.dynamicPricingConfigured && isOn}
                                value={String(local.discoveryNumResults || getCurrentDefaults().discoveryNumResults || "")}
                                helpText={
                                  product.dynamicPricingConfigured && isOn
                                    ? "Locked while dynamic pricing is on — pause to change."
                                    : `1–50 products per discovery run. Shop default: ${getCurrentDefaults().discoveryNumResults}`
                                }
                                onInput={(e) =>
                                  setOverrideField(product.id, "discoveryNumResults", e.currentTarget.value)
                                }
                              />
                            </div>

                            <div>
                              <s-text-field
                                label="Products per listing page"
                                type="number"
                                min="1"
                                max="50"
                                disabled={product.dynamicPricingConfigured && isOn}
                                value={String(local.listingExpansionCap || getCurrentDefaults().listingExpansionCap || "")}
                                helpText={
                                  product.dynamicPricingConfigured && isOn
                                    ? "Locked while dynamic pricing is on — pause to change."
                                    : `When a discovered URL is a listing page, expand this many products (1–50). Shop default: ${getCurrentDefaults().listingExpansionCap}`
                                }
                                onInput={(e) =>
                                  setOverrideField(product.id, "listingExpansionCap", e.currentTarget.value)
                                }
                              />
                            </div>
                          </div>

                          <s-divider />

                          {/* === SECTION 3: Pricing Rules === */}
                          <div>
                            <s-text emphasis="bold">Pricing Rules</s-text>

                            <div style={{ marginBottom: "12px" }}>
                              <s-text tone="subdued" style={SECTION_HELP_TEXT_STYLE}>
                                Which strategy should we use to price relative to competitors?
                              </s-text>
                              <s-stack direction="inline" gap="base" alignItems="center">
                                {["BUDGET", "COMPETITIVE", "PREMIUM"].map((t) => (
                                  <label key={t} style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
                                    <input
                                      type="radio"
                                      name={`tier-${product.id}`}
                                      value={t}
                                      checked={(local.pricingTier ?? "COMPETITIVE") === t}
                                      onChange={() => setOverrideField(product.id, "pricingTier", t)}
                                    />
                                    {t.charAt(0) + t.slice(1).toLowerCase()}
                                  </label>
                                ))}
                              </s-stack>
                            </div>

                            <div>
                              <s-text tone="subdued" style={SECTION_HELP_TEXT_STYLE}>
                                Hard price bounds
                              </s-text>
                              {local.calculatedMinPrice !== null && local.calculatedMaxPrice !== null && (
                                <s-text tone="subdued" style={{ fontSize: "0.85em", marginBottom: "12px" }}>
                                  {(() => {
                                    const sym = getCurrencySymbol(shopDefaults?.currency);
                                    const basisLabel = local.avgBasePrice
                                      ? `Base price: ${sym}${Number(local.avgBasePrice).toFixed(2)}.`
                                      : `Current price: ${sym}${Number(local.price).toFixed(2)}.`;
                                    const capPct = Math.round((shopDefaults?.lifetimeCapPct ?? 0.25) * 100);
                                    return `${basisLabel} Auto-calculated bounds (±${capPct}%): ${sym}${local.calculatedMinPrice.toFixed(2)} to ${sym}${local.calculatedMaxPrice.toFixed(2)}`;
                                  })()}
                                </s-text>
                              )}
                              <s-stack direction="inline" gap="base">
                                <s-text-field
                                  label="Minimum override"
                                  type="number"
                                  value={local.minPriceOverride || (local.calculatedMinPrice ? local.calculatedMinPrice.toFixed(2) : "")}
                                  helpText={`Auto-calculated: ${getCurrencySymbol(shopDefaults?.currency)}${local.calculatedMinPrice?.toFixed(2) ?? "—"}`}
                                  onInput={(e) =>
                                    setOverrideField(product.id, "minPriceOverride", e.currentTarget.value)
                                  }
                                />
                                <s-text-field
                                  label="Maximum override"
                                  type="number"
                                  value={local.maxPriceOverride || (local.calculatedMaxPrice ? local.calculatedMaxPrice.toFixed(2) : "")}
                                  helpText={`Auto-calculated: ${getCurrencySymbol(shopDefaults?.currency)}${local.calculatedMaxPrice?.toFixed(2) ?? "—"}`}
                                  onInput={(e) =>
                                    setOverrideField(product.id, "maxPriceOverride", e.currentTarget.value)
                                  }
                                />
                              </s-stack>
                              {local.boundsError && (
                                <s-text tone="critical" style={{ fontSize: "0.85em" }}>
                                  {local.boundsError}
                                </s-text>
                              )}
                              {!local.boundsError &&
                                Number(local.maxPriceOverride) > 0 &&
                                Number(local.price) > 0 &&
                                Number(local.maxPriceOverride) < Number(local.price) && (
                                <s-text tone="caution" style={{ fontSize: "0.85em" }}>
                                  Maximum ({getCurrencySymbol(shopDefaults?.currency)}{Number(local.maxPriceOverride).toFixed(2)}) is below the current price ({getCurrencySymbol(shopDefaults?.currency)}{Number(local.price).toFixed(2)}) — saving will force a price drop on the next cycle.
                                </s-text>
                              )}
                            </div>
                          </div>

                          <s-divider />

                          {/* === SECTION 5: Rescrape Frequency === */}
                          <div>
                            <s-text tone="subdued" style={SECTION_HELP_TEXT_STYLE}>
                              How often should we re-check for competitor price changes?
                            </s-text>
                            <s-stack direction="inline" gap="base">
                              <div>
                                <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
                                  <label style={{ fontWeight: "500" }}>Every</label>
                                  <span title="Rescrape interval for this product. Leave empty to use shop default." style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: "18px", height: "18px", background: "#e8f0f7", border: "1px solid #b3d9f2", borderRadius: "50%", color: "#0066cc", fontSize: "12px", fontWeight: "bold", cursor: "help" }}>ⓘ</span>
                                </div>
                                <s-text-field
                                  type="number"
                                  value={String(local.frequencyInterval || getCurrentDefaults().frequencyInterval || "")}
                                  helpText={`Shop default: ${getCurrentDefaults().frequencyInterval}`}
                                  onInput={(e) =>
                                    setOverrideField(product.id, "frequencyInterval", e.currentTarget.value)
                                  }
                                />
                              </div>
                              <div>
                                <div style={{ display: "flex", alignItems: "center", marginBottom: "8px" }}>
                                  <label style={{ fontWeight: "500" }}>Unit</label>
                                </div>
                                <s-select
                                  value={local.frequencyUnit || getCurrentDefaults().frequencyUnit || ""}
                                  onChange={(e) =>
                                    setOverrideField(product.id, "frequencyUnit", e.currentTarget.value)
                                  }
                                >
                                  {FREQ_UNITS.map((u) => (
                                    <s-option key={u.value} value={u.value}>{u.label}</s-option>
                                  ))}
                                </s-select>
                              </div>
                            </s-stack>
                          </div>

                          {/* === ACTION BUTTONS (at bottom, after all inputs) === */}
                          <s-divider />

                          <s-stack direction="inline" gap="base" alignItems="center">
                            <s-button
                              variant="primary"
                              onClick={() => submitOverrides(product.id, { enable: !isOn })}
                            >
                              {isOn
                                ? "Save changes"
                                : (local.frequencyUnit && local.frequencyUnit !== "" && local.frequencyUnit !== "never")
                                ? "Save & Resume"
                                : "Save & Enable Dynamic Pricing"}
                            </s-button>
                            <s-link href={`/app/stats/${encodeURIComponent(product.id)}`}>
                              Stats &amp; price history
                            </s-link>
                          </s-stack>

                          {local.saveError && (
                            <s-text tone="critical" style={{ fontSize: "0.85em" }}>
                              {local.saveError}
                            </s-text>
                          )}

                          {!isOn && (
                            <s-text tone="subdued">
                              When you save, discovery begins within ~30s and competitor scraping starts on the schedule you set.
                            </s-text>
                          )}
                        </s-stack>
                      </s-box>
                    </div>
                  )}
                </s-box>
              );

              return idx === 0 ? [row] : [<s-divider key={`div-${product.id}`} />, row];
              })}
            </s-stack>

            {/* Pagination Controls */}
            <div style={{ display: "flex", justifyContent: "center", gap: "8px", padding: "16px", borderTop: "1px solid #f0f0f0", fontSize: "12px" }}>
              <s-button
                size="slim"
                variant="plain"
                onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}
                disabled={currentPage === 1}
              >
                ← Prev
              </s-button>

              {Array.from({ length: totalPages }, (_, i) => i + 1).map((page) => (
                <s-button
                  key={page}
                  size="slim"
                  variant={currentPage === page ? "primary" : "plain"}
                  onClick={() => setCurrentPage(page)}
                  style={{ minWidth: "40px" }}
                >
                  {page}
                </s-button>
              ))}

              <s-button
                size="slim"
                variant="plain"
                onClick={() => setCurrentPage(Math.min(totalPages, currentPage + 1))}
                disabled={currentPage === totalPages}
              >
                Next →
              </s-button>
            </div>
          </>
        )}

        {/* Delete Confirmation Modal */}
        <s-modal id="delete-dp-modal" heading="Delete Dynamic Pricing?" size="small">
          <s-text>
            This will permanently delete all dynamic pricing configuration, competitor matches, and scraped product data for this product. This action cannot be undone.
          </s-text>
          <s-button slot="secondary-actions" commandFor="delete-dp-modal" command="--hide" onClick={() => setDeleteConfirmId(null)}>
            Cancel
          </s-button>
          <s-button slot="primary-action" variant="primary" tone="critical" commandFor="delete-dp-modal" command="--hide" onClick={() => confirmDelete(deleteConfirmId)}>
            Delete with Data
          </s-button>
        </s-modal>
      </s-section>
    </s-page>
  );
}

export const headers = (headersArgs) => {
  return boundary.headers(headersArgs);
};
