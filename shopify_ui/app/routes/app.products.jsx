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

  // Ensure ShopifyUser row exists for this shop
  await db.shopifyUser.upsert({
    where: { shopDomain },
    update: {},
    create: { shopDomain },
  });

  const products = await db.shopifyProduct.findMany({
    where: { shopDomain },
    include: {
      ShopifyVariant: { take: 1 },
      _count: { select: { CompetitorCandidate: true, ProductLevelMatch: true } },
    },
    orderBy: { updatedAt: "desc" },
  });

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

  const freshUser = await db.shopifyUser.findUnique({ where: { shopDomain } });
  const shopSettings = await db.shopSettings.findUnique({ where: { shopDomain } });

  const processingCount = await db.shopifyProduct.count({
    where: {
      shopDomain,
      updatedAt: { gte: new Date(Date.now() - 15 * 60 * 1000) },
      ShopifyVariant: { some: { semanticText: null } },
    },
  });

  // With expiring offline tokens enabled, individual expiry is normal —
  // the library auto-refreshes via Token Exchange on the next call. The
  // only state worth banner-ing is "no offline session row at all", which
  // means install never completed (or was wiped).
  const hasOfflineSession = !!(await db.session.findFirst({
    where: { shop: shopDomain, isOnline: false },
    select: { id: true },
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
      { method: "POST" },
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ shop_domain: shopDomain, product_id: productId, config }),
    });
    const data = await res.json();
    return data.ok ? { ok: true } : { ok: false, error: data.error };
  }

  if (intent === "pauseDynamic") {
    const res = await fetch(`${PYTHON_API_URL}/internal/dynamic-pricing/pause`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ shop_domain: shopDomain, product_id: productId }),
    });
    const data = await res.json();
    return data.ok ? { ok: true } : { ok: false, error: data.error };
  }

  if (intent === "resumeDynamic") {
    const res = await fetch(`${PYTHON_API_URL}/internal/dynamic-pricing/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ shop_domain: shopDomain, product_id: productId }),
    });
    const data = await res.json();
    return data.ok ? { ok: true } : { ok: false, error: data.error };
  }

  if (intent === "deleteDynamicWithData") {
    const res = await fetch(`${PYTHON_API_URL}/internal/dynamic-pricing/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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

  const handleDeleteWithData = (productId) => {
    setDeleteConfirmId(productId);
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
          <s-stack direction="block" gap="tight" align="center">
            <s-text emphasis="bold">No products match your filters</s-text>
            <s-text tone="subdued">
              Try clearing the search or selecting a different tag or category.
            </s-text>
          </s-stack>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "0", border: "1px solid #ddd", borderRadius: "4px", overflow: "hidden" }}>
            {/* Table Header */}
            <div style={{ display: "flex", padding: "12px 16px", borderBottom: "2px solid #f0f0f0", background: "#f9f9f9", fontWeight: "bold", fontSize: "12px", color: "#666", gap: "16px", alignItems: "center" }}>
              <div style={{ width: "50px" }}>Image</div>
              <div style={{ flex: 1, minWidth: "220px" }}>Product Name</div>
              <div style={{ width: "70px" }}>Price</div>
              <div style={{ width: "80px" }}>DP Status</div>
              <div style={{ width: "100px" }}>Rescrape</div>
              <div style={{ width: "80px" }}>Matches</div>
              <div style={{ width: "60px" }}></div>
            </div>

            {/* Product Rows */}
            {paginatedProducts.map((product) => {
              const local = getLocal(product.id);
              const isOn = local.dynamicPricingEnabled;
              const isExpanded = expandedId === product.id;

              return (
                <div key={product.id}>
                  {/* Product Row */}
                  <div style={{ display: "flex", padding: "12px 16px", borderBottom: "1px solid #f0f0f0", alignItems: "center", gap: "16px", background: "white" }}>
                    {product.imageUrl && (
                      <img
                        src={product.imageUrl}
                        alt={product.title}
                        width="50"
                        height="50"
                        style={{ objectFit: "cover", borderRadius: "4px", flex: "0 0 50px" }}
                      />
                    )}
                    {!product.imageUrl && (
                      <div style={{ width: "50px", height: "50px", background: "#ddd", borderRadius: "4px", flex: "0 0 50px" }} />
                    )}

                    <div style={{ flex: 1, minWidth: "220px" }}>
                      <div style={{ fontWeight: "500", marginBottom: "4px" }}>{product.title}</div>
                      <div style={{ fontSize: "12px", color: "#666" }}>{product.productType || "Product"}</div>
                    </div>

                    <div style={{ width: "70px", fontWeight: "500" }}>{getCurrencySymbol(shopDefaults?.currency)}{product.price}</div>

                    <div style={{ width: "80px", display: "flex", gap: "6px", alignItems: "center" }}>
                      {isOn ? (
                        <>
                          <span style={{ background: "#4CAF50", color: "white", padding: "4px 8px", borderRadius: "3px", fontSize: "11px" }}>ON</span>
                          <div style={{ position: "relative" }}>
                            <s-button
                              variant="plain"
                              size="slim"
                              onClick={() => setOpenMenuId(openMenuId === product.id ? null : product.id)}
                              style={{ fontSize: "10px", padding: "0 4px" }}
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
                                {local.frequencyUnit && local.frequencyUnit !== "never" && (
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
                                <button
                                  onClick={() => {
                                    handleDeleteWithData(product.id);
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
                        </>
                      ) : local.frequencyUnit && local.frequencyUnit !== "never" ? (
                        <span style={{ background: "#FF9800", color: "white", padding: "4px 8px", borderRadius: "3px", fontSize: "11px" }}>Pause</span>
                      ) : (
                        <span style={{ background: "#ccc", color: "#666", padding: "4px 8px", borderRadius: "3px", fontSize: "11px" }}>OFF</span>
                      )}
                    </div>

                    <div style={{ width: "100px", fontSize: "12px" }}>
                      {isOn && local.frequencyUnit && local.frequencyUnit !== "never"
                        ? `${local.frequencyInterval || ""} ${local.frequencyUnit}`
                        : !isOn && local.frequencyUnit && local.frequencyUnit !== "never"
                        ? <s-button
                            size="slim"
                            variant="secondary"
                            onClick={() => {
                              handleResume(product.id);
                              setOpenMenuId(null);
                            }}
                            style={{ fontSize: "11px" }}
                          >
                            Resume
                          </s-button>
                        : "–"}
                    </div>

                    <div style={{ width: "80px", fontSize: "12px" }}>{product.matchCount || 0}</div>

                    <div style={{ width: "60px" }}>
                      <s-button
                        variant="plain"
                        size="slim"
                        onClick={() => toggleExpand(product.id)}
                        style={{ fontSize: "11px" }}
                      >
                        {isExpanded ? "▾ Hide" : "▸ Details"}
                      </s-button>
                    </div>
                  </div>

                  {/* Expanded Details Panel */}
                  {isExpanded && (
                    <div style={{ padding: "20px 16px", borderBottom: "1px solid #f0f0f0", background: "#f9f9f9" }}>
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
                              <s-stack direction="inline" gap="base" align="center">
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

                          <s-stack direction="inline" gap="base" align="center">
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
                </div>
              );
            })}

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
          </div>
        )}

        {/* Delete Confirmation Modal */}
        {deleteConfirmId && (
          <div style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
          }}>
            <div style={{
              background: "white",
              borderRadius: "8px",
              padding: "24px",
              maxWidth: "400px",
              boxShadow: "0 4px 12px rgba(0,0,0,0.15)",
            }}>
              <h3 style={{ margin: "0 0 12px 0", fontSize: "16px", fontWeight: "600" }}>
                Delete Dynamic Pricing?
              </h3>
              <p style={{ margin: "0 0 20px 0", fontSize: "14px", color: "#666" }}>
                This will permanently delete all dynamic pricing configuration, competitor matches, and scraped product data for this product. This action cannot be undone.
              </p>
              <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
                <button
                  onClick={() => setDeleteConfirmId(null)}
                  style={{
                    padding: "8px 16px",
                    border: "1px solid #ddd",
                    borderRadius: "4px",
                    background: "white",
                    cursor: "pointer",
                    fontSize: "14px",
                  }}
                >
                  Cancel
                </button>
                <button
                  onClick={() => confirmDelete(deleteConfirmId)}
                  style={{
                    padding: "8px 16px",
                    border: "none",
                    borderRadius: "4px",
                    background: "#d32f2f",
                    color: "white",
                    cursor: "pointer",
                    fontSize: "14px",
                  }}
                >
                  Delete with Data
                </button>
              </div>
            </div>
          </div>
        )}
      </s-section>
    </s-page>
  );
}

export const headers = (headersArgs) => {
  return boundary.headers(headersArgs);
};
