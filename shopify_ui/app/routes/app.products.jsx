import { useState, useMemo, useEffect } from "react";
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
    syncPrice: p.syncPrice,
    searchQuery: p.searchQuery ?? "",
    searchQueryOverride: p.searchQueryOverride ?? "",
    floorPrice: p.floorPrice?.toString() ?? "",
    ceilingPrice: p.ceilingPrice?.toString() ?? "",
    pricingTier: p.pricingTier ?? "COMPETITIVE",
    basePrice: p.basePrice?.toString() ?? null,
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

// ─── Action ───────────────────────────────────────────────────────────────────
export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const formData = await request.formData();
  const intent = formData.get("intent");
  const productId = formData.get("productId");

  if (intent === "syncProducts") {
    const shopDomain = session.shop;
    const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";
    const user = await db.shopifyUser.findUnique({ where: { shopDomain } });

    // Guard against double-trigger: if a sync started < 10 min ago, no-op.
    const startedAt = user?.productSyncStartedAt?.getTime() ?? 0;
    const recentlyStarted = user?.productSyncState === "SYNCING"
      && Date.now() - startedAt < 10 * 60 * 1000;
    if (!recentlyStarted) {
      await db.shopifyUser.update({
        where: { shopDomain },
        data: { productSyncState: "SYNCING", productSyncStartedAt: new Date() },
      });
      void fetch(
        `${PYTHON_API_URL}/internal/shopify/sync-products?shop_domain=${encodeURIComponent(shopDomain)}`,
        { method: "POST" },
      ).catch(() => {});
    }
    return { ok: true };
  }

  if (intent === "toggleDynamic") {
    // Pause/resume model. The beat filter is the actual gate: it only
    // dispatches rescrape when dynamicPricingEnabled=TRUE.
    const enabled = formData.get("enabled") === "true";

    const updateData = { dynamicPricingEnabled: enabled };

    if (enabled) {
      // Snapshot basePrice on FIRST enable (both at product- and variant-level)
      // so the lifetime cap has a stable anchor. Preserved across toggle off→on.
      const product = await db.shopifyProduct.findUnique({
        where: { id: productId },
        select: { basePrice: true, ShopifyVariant: { select: { id: true, currentPrice: true, basePrice: true } } },
      });
      if (product && product.basePrice == null) {
        const minVariantPrice = product.ShopifyVariant
          .map((v) => Number(v.currentPrice))
          .filter((n) => Number.isFinite(n) && n > 0)
          .reduce((a, b) => Math.min(a, b), Number.POSITIVE_INFINITY);
        if (Number.isFinite(minVariantPrice)) {
          updateData.basePrice = minVariantPrice;
        }
      }
      // Per-variant snapshot — only fills nulls.
      for (const v of product?.ShopifyVariant ?? []) {
        if (v.basePrice == null && Number(v.currentPrice) > 0) {
          await db.shopifyVariant.update({
            where: { id: v.id },
            data: { basePrice: v.currentPrice },
          });
        }
      }
    } else {
      // When turning off DP, clear all product-specific setting overrides
      // so they revert to shop defaults when re-enabled.
      updateData.frequencyInterval = null;
      updateData.frequencyUnit = null;
      updateData.floorPrice = null;
      updateData.ceilingPrice = null;
      updateData.pricingTier = "COMPETITIVE";
      updateData.minPriceOverride = null;
      updateData.maxPriceOverride = null;
      updateData.searchQueryOverride = null;
      updateData.listingExpansionCap = null;
      updateData.discoveryNumResults = null;
    }

    await db.shopifyProduct.update({
      where: { id: productId },
      data: updateData,
    });

    if (enabled) {
      // Resume: re-arm any URLs we already discovered so they enter the
      // rescrape loop on the next beat tick. Only touches active rows whose
      // current schedule has gone stale (nextRunAt NULL or in the past) so
      // we don't trample a healthy upcoming schedule.
      await db.productUrl.updateMany({
        where: {
          shopifyProductId: productId,
          status: "ACTIVE",
          OR: [{ nextRunAt: null }, { nextRunAt: { lte: new Date() } }],
        },
        data: { nextRunAt: new Date() },
      });
    }
  } else if (intent === "toggleRescrape") {
    // Per-product rescrape toggle. OFF parks frequencyUnit at 'never';
    // ON restores a sensible default cadence if the product was 'never'
    // (so the merchant can flip it on without first opening the editor).
    const enabled = formData.get("enabled") === "true";
    const current = await db.shopifyProduct.findUnique({
      where: { id: productId },
      select: { frequencyUnit: true, frequencyInterval: true },
    });
    if (enabled) {
      const defaultsNeeded =
        !current?.frequencyUnit || current.frequencyUnit === "never";
      await db.shopifyProduct.update({
        where: { id: productId },
        data: defaultsNeeded
          ? { frequencyUnit: "hour", frequencyInterval: 6 }
          : {},
      });
      // Re-arm stale schedules so beat picks the URLs up on the next tick.
      await db.productUrl.updateMany({
        where: {
          shopifyProductId: productId,
          status: "ACTIVE",
          OR: [{ nextRunAt: null }, { nextRunAt: { lte: new Date() } }],
        },
        data: { nextRunAt: new Date() },
      });
    } else {
      await db.shopifyProduct.update({
        where: { id: productId },
        data: { frequencyUnit: "never", frequencyInterval: null },
      });
    }
  } else if (intent === "saveAndEnable" || intent === "updateOverrides") {
    // Strict validation: frequencyUnit must be a canonical option; floor/ceiling
    // must parse as positive numbers when provided.
    const allowedUnits = new Set(["never", "minute", "hour", "day"]);
    const rawUnit     = formData.get("frequencyUnit") || "";
    const rawInterval = formData.get("frequencyInterval");
    const rawFloor    = formData.get("floorPrice");
    const rawCeiling  = formData.get("ceilingPrice");
    const rawOverride = (formData.get("searchQueryOverride") || "").toString().trim();
    const rawNumResults = formData.get("discoveryNumResults");
    const rawListingCap = formData.get("listingExpansionCap");

    const data = {
      searchQueryOverride: rawOverride === "" ? null : rawOverride,
    };

    if (rawUnit && allowedUnits.has(rawUnit)) {
      data.frequencyUnit = rawUnit;
    } else if (rawUnit === "") {
      data.frequencyUnit = null;
    }

    if (rawInterval === "" || rawInterval === null) {
      data.frequencyInterval = null;
    } else {
      const n = parseInt(rawInterval, 10);
      if (Number.isFinite(n) && n > 0) data.frequencyInterval = n;
    }

    const parseDecimal = (raw) => {
      if (raw === "" || raw === null) return null;
      const n = parseFloat(raw);
      return Number.isFinite(n) && n >= 0 ? n : undefined;
    };

    const floor   = parseDecimal(rawFloor);
    const ceiling = parseDecimal(rawCeiling);
    if (floor   !== undefined) data.floorPrice   = floor;
    if (ceiling !== undefined) data.ceilingPrice = ceiling;

    // Auto-pricing per-product overrides. Tier is an enum; min/max override
    // win over the basePrice × lifetimeCapPct fallback when present.
    const rawTier = (formData.get("pricingTier") || "").toString();
    if (["BUDGET", "COMPETITIVE", "PREMIUM"].includes(rawTier)) {
      data.pricingTier = rawTier;
    }
    const minOverride = parseDecimal(formData.get("minPriceOverride"));
    const maxOverride = parseDecimal(formData.get("maxPriceOverride"));
    if (minOverride !== undefined) data.minPriceOverride = minOverride;
    if (maxOverride !== undefined) data.maxPriceOverride = maxOverride;

    if (rawNumResults !== null && rawNumResults !== "") {
      const n = parseInt(rawNumResults, 10);
      if (Number.isFinite(n) && n > 0) data.discoveryNumResults = Math.min(n, 50);
    }

    if (rawListingCap !== null && rawListingCap !== "") {
      const n = parseInt(rawListingCap, 10);
      if (Number.isFinite(n) && n > 0) data.listingExpansionCap = Math.min(n, 50);
    }

    if (intent === "saveAndEnable") {
      data.dynamicPricingEnabled = true;
    }

    // Detect a "rescrape just got turned on" transition so we can re-arm
    // ProductUrl.nextRunAt afterwards. Triggers when frequencyUnit moves
    // from null/'never' to a real cadence.
    const prior = await db.shopifyProduct.findUnique({
      where: { id: productId },
      select: { frequencyUnit: true },
    });
    const priorUnit = prior?.frequencyUnit ?? null;
    const newUnit   = data.frequencyUnit ?? priorUnit;
    const rescrapeJustEnabled =
      (priorUnit === null || priorUnit === "never") &&
      newUnit && newUnit !== "never";

    await db.shopifyProduct.update({
      where: { id: productId },
      data,
    });

    if (rescrapeJustEnabled || intent === "saveAndEnable") {
      // Re-arm existing URLs so they enter the rescrape loop without
      // waiting for a fresh discovery pass. Same conservative filter as
      // toggleDynamic — only touches stale/missing schedules.
      await db.productUrl.updateMany({
        where: {
          shopifyProductId: productId,
          status: "ACTIVE",
          OR: [{ nextRunAt: null }, { nextRunAt: { lte: new Date() } }],
        },
        data: { nextRunAt: new Date() },
      });
    }
  } else if (intent === "pauseDynamic") {
    // Pause DP but keep all config intact — just disable the flag.
    // If frequency is set, rescraping will stop on next beat tick.
    await db.shopifyProduct.update({
      where: { id: productId },
      data: { dynamicPricingEnabled: false },
    });
  } else if (intent === "resumeDynamic") {
    // Resume paused DP — re-enable the flag and re-arm stale schedules.
    await db.shopifyProduct.update({
      where: { id: productId },
      data: { dynamicPricingEnabled: true },
    });
    // Re-arm any URLs with stale schedules so beat picks them up on next tick.
    await db.productUrl.updateMany({
      where: {
        shopifyProductId: productId,
        status: "ACTIVE",
        OR: [{ nextRunAt: null }, { nextRunAt: { lte: new Date() } }],
      },
      data: { nextRunAt: new Date() },
    });
  } else if (intent === "deleteDynamicWithData") {
    try {
      // Get all variants for this product
      const variants = await db.shopifyVariant.findMany({
        where: { productId },
        select: { id: true },
      });
      const variantIds = variants.map((v) => v.id);

      // 1. Delete variant-level data (VariantCompetitorStats, PriceDecision)
      if (variantIds.length > 0) {
        await db.variantCompetitorStats.deleteMany({
          where: { shopifyVariantId: { in: variantIds } },
        });
        await db.priceDecision.deleteMany({
          where: { shopifyVariantId: { in: variantIds } },
        });
      }

      // 2. Delete ProductMatch (matches between shopify variants and competitor variants)
      if (variantIds.length > 0) {
        await db.productMatch.deleteMany({
          where: { shopifyVariantId: { in: variantIds } },
        });
      }

      // 3. Delete ProductLevelMatch (product-level)
      await db.productLevelMatch.deleteMany({
        where: { shopifyProductId: productId },
      });

      // 4. Delete ProductUrl and get ScrapingConfigs to delete
      const productUrls = await db.productUrl.findMany({
        where: { shopifyProductId: productId },
        select: { configId: true },
      });
      const configIds = productUrls.map((pu) => pu.configId).filter(Boolean);
      await db.productUrl.deleteMany({
        where: { shopifyProductId: productId },
      });

      // 5. Delete ScrapingConfigs
      if (configIds.length > 0) {
        await db.scrapingConfig.deleteMany({
          where: { id: { in: configIds } },
        });
      }

      // 6. Delete DiscoveryJob and CompetitorCandidate
      await db.discoveryJob.deleteMany({
        where: { shopifyProductId: productId },
      });
      await db.competitorCandidate.deleteMany({
        where: { shopifyProductId: productId },
      });

      // 7. Delete ScrapedProducts and related data
      const scrapedProducts = await db.scrapedProduct.findMany({
        where: { CompetitorCandidate: { some: { shopifyProductId: productId } } },
        select: { id: true },
      });
      const scrapedIds = scrapedProducts.map((s) => s.id);

      if (scrapedIds.length > 0) {
        await db.scrapedVariant.deleteMany({
          where: { productId: { in: scrapedIds } },
        });
        await db.productEmbedding.deleteMany({
          where: { prodId: { in: scrapedIds } },
        });
        await db.scrapedProduct.deleteMany({
          where: { id: { in: scrapedIds } },
        });
      }

      // 8. Clear DP config on ShopifyProduct
      await db.shopifyProduct.update({
        where: { id: productId },
        data: {
          dynamicPricingEnabled: false,
          frequencyInterval: null,
          frequencyUnit: null,
          floorPrice: null,
          ceilingPrice: null,
          pricingTier: "COMPETITIVE",
          minPriceOverride: null,
          maxPriceOverride: null,
          searchQueryOverride: null,
          listingExpansionCap: null,
          discoveryNumResults: null,
          basePrice: null,
        },
      });
    } catch (error) {
      console.error("Delete DP error:", error);
      throw new Error(`Failed to delete dynamic pricing data: ${error.message}`);
    }
  }

  return null;
};

// ─── UI ───────────────────────────────────────────────────────────────────────
export default function HomePage() {
  const { products, auth, productSyncState, productSyncedAt, processingCount, shopDefaults } = useLoaderData();
  const fetcher = useFetcher();
  const revalidator = useRevalidator();
  const syncFetcher = useFetcher();

  const isBusy = productSyncState === "SYNCING" || processingCount > 0;
  useEffect(() => {
    if (!isBusy) return;
    const t = setInterval(() => revalidator.revalidate(), 2000);
    return () => clearInterval(t);
  }, [isBusy, revalidator]);

  const [searchQuery, setSearchQuery] = useState("");
  const [selectedTag, setSelectedTag] = useState("all");
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
        floorPrice: p.floorPrice,
        ceilingPrice: p.ceilingPrice,
        pricingTier: p.pricingTier ?? "COMPETITIVE",
        basePrice: p.basePrice,
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
      const priceForCalculation = local.basePrice || local.price;
      if (product && priceForCalculation) {
        const lifetimeCapPct = shopDefaults?.lifetimeCapPct ?? 0.25;
        const { min, max } = calculateMinMaxPrices(Number(priceForCalculation), lifetimeCapPct);
        setLocalState((prev) => ({
          ...prev,
          [expandedId]: {
            ...prev[expandedId],
            calculatedMinPrice: min,
            calculatedMaxPrice: max,
          },
        }));
      }
    }
  }, [expandedId, products, shopDefaults]);

  const allTags = useMemo(() => {
    const tagSet = new Set();
    for (const p of products) {
      if (Array.isArray(p.tags)) p.tags.forEach((t) => tagSet.add(t));
    }
    return [...tagSet].sort();
  }, [products]);

  const allCategories = useMemo(() => {
    const catSet = new Set(products.map((p) => p.productType).filter(Boolean));
    return [...catSet].sort();
  }, [products]);

  const filteredProducts = useMemo(() => {
    return products.filter((p) => {
      const matchesSearch = p.title.toLowerCase().includes(searchQuery.toLowerCase());
      const productTags = (() => {
        try { return JSON.parse(p.tags); } catch { return []; }
      })();
      const matchesTag = selectedTag === "all" || productTags.includes(selectedTag);
      const matchesCategory = selectedCategory === "all" || p.productType === selectedCategory;
      return matchesSearch && matchesTag && matchesCategory;
    });
  }, [products, searchQuery, selectedTag, selectedCategory]);

  const paginatedProducts = useMemo(() => {
    const startIndex = (currentPage - 1) * itemsPerPage;
    const endIndex = startIndex + itemsPerPage;
    return filteredProducts.slice(startIndex, endIndex);
  }, [filteredProducts, currentPage, itemsPerPage]);

  const totalPages = Math.ceil(filteredProducts.length / itemsPerPage);

  useEffect(() => {
    setCurrentPage(1);
  }, [searchQuery, selectedCategory, selectedTag]);

  const getLocal = (id) =>
    localState[id] ?? {
      dynamicPricingEnabled: false,
      syncPrice: true,
      searchQueryOverride: "",
      floorPrice: "",
      ceilingPrice: "",
      pricingTier: "COMPETITIVE",
      basePrice: null,
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
    const intent = opts.enable ? "saveAndEnable" : "updateOverrides";
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
        floorPrice:          local.floorPrice ?? "",
        ceilingPrice:        local.ceilingPrice ?? "",
        pricingTier:         local.pricingTier ?? "COMPETITIVE",
        minPriceOverride:    local.minPriceOverride ?? "",
        maxPriceOverride:    local.maxPriceOverride ?? "",
        frequencyInterval:   local.frequencyInterval ?? "",
        frequencyUnit:       local.frequencyUnit ?? "",
        discoveryNumResults: String(local.discoveryNumResults ?? ""),
        listingExpansionCap: String(local.listingExpansionCap ?? ""),
      },
      { method: "POST" },
    );
  };

  // Toggle now opens the panel for enabling (commit deferred to Save) or
  // disables immediately (no panel data to commit on the way down).
  const handleToggle = (productId, currentValue) => {
    if (currentValue) {
      // Disable path — clear product-specific overrides and commit immediately.
      setLocalState((prev) => ({
        ...prev,
        [productId]: {
          ...prev[productId],
          dynamicPricingEnabled: false,
          frequencyInterval: "",
          frequencyUnit: "",
          floorPrice: "",
          ceilingPrice: "",
          pricingTier: "COMPETITIVE",
          minPriceOverride: "",
          maxPriceOverride: "",
          searchQueryOverride: "",
          listingExpansionCap: "",
          discoveryNumResults: "",
        },
      }));
      fetcher.submit(
        { intent: "toggleDynamic", productId, enabled: "false" },
        { method: "POST" },
      );
    } else {
      // Enable path — just open the panel. Save button commits everything.
      setExpandedId(productId);
    }
  };

  const handlePause = (productId) => {
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
    setLocalState((prev) => ({
      ...prev,
      [productId]: {
        ...prev[productId],
        dynamicPricingEnabled: false,
        frequencyInterval: "",
        frequencyUnit: "",
        floorPrice: "",
        ceilingPrice: "",
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

  const handleRescrapeToggle = (productId, currentlyOn) => {
    const nextUnit = currentlyOn ? "never" : "hour";
    setLocalState((prev) => {
      const local = prev[productId] ?? getLocal(productId);
      return {
        ...prev,
        [productId]: {
          ...local,
          frequencyUnit: nextUnit,
          frequencyInterval: currentlyOn ? null : (local.frequencyInterval ?? 6),
        },
      };
    });
    fetcher.submit(
      { intent: "toggleRescrape", productId, enabled: currentlyOn ? "false" : "true" },
      { method: "POST" },
    );
  };

  /* REMOVED: handleFieldChange function
     - Was used to toggle sync options (Price/Description/Title)
     - Only price syncing is active now; no UI for selecting fields
  */

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
          {/* <s-select
            label="Tag"
            value={selectedTag}
            onChange={(e) => setSelectedTag(e.currentTarget.value)}
          >
            <s-option value="all">All Tags</s-option>
            {allTags.map((tag) => (
              <s-option key={tag} value={tag}>{tag}</s-option>
            ))}
          </s-select> */}


          
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
                                value={String(local.discoveryNumResults || getCurrentDefaults().discoveryNumResults || "")}
                                helpText={`1–50 products per discovery run. Shop default: ${getCurrentDefaults().discoveryNumResults}`}
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
                                value={String(local.listingExpansionCap || getCurrentDefaults().listingExpansionCap || "")}
                                helpText={`When a discovered URL is a listing page, expand this many products (1–50). Shop default: ${getCurrentDefaults().listingExpansionCap}`}
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
                                  {local.basePrice ? `Base price: ${getCurrencySymbol(shopDefaults?.currency)}${Number(local.basePrice).toFixed(2)}.` : `Current price: ${getCurrencySymbol(shopDefaults?.currency)}${Number(local.price).toFixed(2)}.`}
                                  Auto-calculated bounds (±{Math.round((shopDefaults?.lifetimeCapPct ?? 0.25) * 100)}%): ${getCurrencySymbol(shopDefaults?.currency)}${local.calculatedMinPrice.toFixed(2)} to ${getCurrencySymbol(shopDefaults?.currency)}${local.calculatedMaxPrice.toFixed(2)}
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
                            </div>
                          </div>

                          <s-divider />

                          {/* REMOVED: Sync to Shopify section (Price/Description/Title options)
                              - Only price syncing is active now
                              - syncPrice is always enabled
                              - syncDescription and syncTitle removed from UI and form handlers
                          */}

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
