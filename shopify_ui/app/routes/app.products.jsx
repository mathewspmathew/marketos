import { useState, useMemo, useEffect } from "react";
import { useFetcher, useLoaderData, useRevalidator } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

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
      variants: { take: 1 },
      _count: { select: { competitorCandidates: true, productLevelMatches: true } },
    },
    orderBy: { updatedAt: "desc" },
  });

  const flattened = products.map((p) => ({
    id: p.id,
    title: p.title,
    productType: p.productType,
    tags: p.tags,
    imageUrl: p.imageUrl,
    price: p.variants[0]?.currentPrice?.toString() ?? "0.00",
    compareAtPrice: p.variants[0]?.compareAtPrice?.toString() ?? null,
    dynamicPricingEnabled: p.dynamicPricingEnabled,
    syncPrice: p.syncPrice,
    syncDescription: p.syncDescription,
    syncTitle: p.syncTitle,
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
    discoveryNumResults: p.discoveryNumResults ?? 10,
    listingExpansionCap: p.listingExpansionCap ?? 5,
    lastDiscoveryAt: p.lastDiscoveryAt ? p.lastDiscoveryAt.toISOString() : null,
    matchCount: p._count.productLevelMatches,
    candidateCount: p._count.competitorCandidates,
  }));

  const freshUser = await db.shopifyUser.findUnique({ where: { shopDomain } });
  const processingCount = await db.shopifyProduct.count({
    where: {
      shopDomain,
      updatedAt: { gte: new Date(Date.now() - 15 * 60 * 1000) },
      variants: { some: { semanticText: null } },
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
    // Pause/resume model. We no longer clear lastDiscoveryAt on toggle-off,
    // so turning the product back on resumes from existing competitor URLs
    // instead of running Serper from scratch. The beat filter is the actual
    // gate: it only dispatches rescrape when dynamicPricingEnabled=TRUE.
    const enabled = formData.get("enabled") === "true";

    // Snapshot basePrice on FIRST enable (both at product- and variant-level)
    // so the lifetime cap has a stable anchor. Preserved across toggle off→on.
    if (enabled) {
      const product = await db.shopifyProduct.findUnique({
        where: { id: productId },
        select: { basePrice: true, variants: { select: { id: true, currentPrice: true, basePrice: true } } },
      });
      if (product && product.basePrice == null) {
        const minVariantPrice = product.variants
          .map((v) => Number(v.currentPrice))
          .filter((n) => Number.isFinite(n) && n > 0)
          .reduce((a, b) => Math.min(a, b), Number.POSITIVE_INFINITY);
        if (Number.isFinite(minVariantPrice)) {
          await db.shopifyProduct.update({
            where: { id: productId },
            data: { basePrice: minVariantPrice },
          });
        }
      }
      // Per-variant snapshot — only fills nulls.
      for (const v of product?.variants ?? []) {
        if (v.basePrice == null && Number(v.currentPrice) > 0) {
          await db.shopifyVariant.update({
            where: { id: v.id },
            data: { basePrice: v.currentPrice },
          });
        }
      }
    }

    await db.shopifyProduct.update({
      where: { id: productId },
      data: { dynamicPricingEnabled: enabled },
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
  } else if (intent === "updateFields") {
    await db.shopifyProduct.update({
      where: { id: productId },
      data: {
        syncPrice: formData.get("syncPrice") === "true",
        syncDescription: formData.get("syncDescription") === "true",
        syncTitle: formData.get("syncTitle") === "true",
      },
    });
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
  }

  return null;
};

// ─── UI ───────────────────────────────────────────────────────────────────────
export default function HomePage() {
  const { products, auth, productSyncState, productSyncedAt, processingCount } = useLoaderData();
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

  const [localState, setLocalState] = useState(() => {
    const map = {};
    for (const p of products) {
      map[p.id] = {
        dynamicPricingEnabled: p.dynamicPricingEnabled,
        syncPrice: p.syncPrice,
        syncDescription: p.syncDescription,
        syncTitle: p.syncTitle,
        searchQueryOverride: p.searchQueryOverride,
        floorPrice: p.floorPrice,
        ceilingPrice: p.ceilingPrice,
        pricingTier: p.pricingTier ?? "COMPETITIVE",
        basePrice: p.basePrice,
        minPriceOverride: p.minPriceOverride,
        maxPriceOverride: p.maxPriceOverride,
        frequencyInterval: p.frequencyInterval === "" ? "" : String(p.frequencyInterval),
        frequencyUnit: p.frequencyUnit,
        discoveryNumResults: p.discoveryNumResults ?? 10,
        listingExpansionCap: p.listingExpansionCap ?? 5,
      };
    }
    return map;
  });

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

  const getLocal = (id) =>
    localState[id] ?? {
      dynamicPricingEnabled: false,
      syncPrice: true,
      syncDescription: false,
      syncTitle: false,
      searchQueryOverride: "",
      floorPrice: "",
      ceilingPrice: "",
      pricingTier: "COMPETITIVE",
      basePrice: null,
      minPriceOverride: "",
      maxPriceOverride: "",
      frequencyInterval: "",
      frequencyUnit: "",
      discoveryNumResults: 10,
      listingExpansionCap: 5,
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
        discoveryNumResults: String(local.discoveryNumResults ?? 10),
        listingExpansionCap: String(local.listingExpansionCap ?? 5),
      },
      { method: "POST" },
    );
  };

  // Toggle now opens the panel for enabling (commit deferred to Save) or
  // disables immediately (no panel data to commit on the way down).
  const handleToggle = (productId, currentValue) => {
    if (currentValue) {
      // Disable path — commit immediately.
      setLocalState((prev) => ({
        ...prev,
        [productId]: { ...prev[productId], dynamicPricingEnabled: false },
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

  const handleFieldChange = (productId, field, currentValue) => {
    const newValue = !currentValue;
    const updated = { ...getLocal(productId), [field]: newValue };
    setLocalState((prev) => ({ ...prev, [productId]: updated }));
    fetcher.submit(
      {
        intent: "updateFields",
        productId,
        syncPrice: String(updated.syncPrice),
        syncDescription: String(updated.syncDescription),
        syncTitle: String(updated.syncTitle),
      },
      { method: "POST" },
    );
  };

  const toggleExpand = (id) => setExpandedId((prev) => (prev === id ? null : id));

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
            {productSyncedAt ? `Synced ✓ ${new Date(productSyncedAt).toLocaleTimeString()}` : "Synced ✓"}
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
            label="Tag"
            value={selectedTag}
            onChange={(e) => setSelectedTag(e.currentTarget.value)}
          >
            <s-option value="all">All Tags</s-option>
            {allTags.map((tag) => (
              <s-option key={tag} value={tag}>{tag}</s-option>
            ))}
          </s-select>
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
          <s-resource-list>
            {filteredProducts.map((product) => {
              const local = getLocal(product.id);
              const isOn = local.dynamicPricingEnabled;
              const isExpanded = expandedId === product.id;
              const productTags = (() => {
                try { return JSON.parse(product.tags); } catch { return []; }
              })();

              return (
                <s-resource-item key={product.id} id={product.id}>
                  {product.imageUrl && (
                    <img
                      slot="media"
                      src={product.imageUrl}
                      alt={product.title}
                      width="50"
                      height="50"
                      style={{ objectFit: "cover", borderRadius: "4px" }}
                    />
                  )}

                  <s-stack direction="block" gap="tight">
                    <s-stack direction="inline" gap="base" align="center">
                      <s-text emphasis="bold">{product.title}</s-text>
                      <s-badge>{product.productType || "Product"}</s-badge>
                      <s-text>${product.price}</s-text>
                      {product.compareAtPrice && (
                        <s-text tone="subdued" style={{ textDecoration: "line-through" }}>
                          ${product.compareAtPrice}
                        </s-text>
                      )}
                    </s-stack>

                    {productTags.length > 0 && (
                      <s-stack direction="inline" gap="tight">
                        {productTags.slice(0, 5).map((tag) => (
                          <s-badge key={tag} tone="info">{tag}</s-badge>
                        ))}
                      </s-stack>
                    )}

                    <s-stack direction="inline" gap="base" align="center">
                      <s-text>Dynamic Pricing</s-text>
                      {isOn ? (
                        <>
                          <s-badge tone="success">ON</s-badge>
                          <s-button
                            size="slim"
                            tone="critical"
                            onClick={() => handleToggle(product.id, true)}
                          >
                            Turn off
                          </s-button>
                        </>
                      ) : (
                        <>
                          <s-badge tone="subdued">OFF</s-badge>
                          <s-button
                            size="slim"
                            variant="primary"
                            onClick={() => setExpandedId(product.id)}
                          >
                            Configure &amp; turn on
                          </s-button>
                        </>
                      )}
                      {(() => {
                        const rescrapeOn =
                          isOn && !!local.frequencyUnit && local.frequencyUnit !== "never";
                        return (
                          <>
                            <s-text>Rescrape</s-text>
                            {rescrapeOn ? (
                              <>
                                <s-badge tone="success">
                                  {`Every ${local.frequencyInterval || ""} ${local.frequencyUnit}`}
                                </s-badge>
                                <s-button
                                  size="slim"
                                  tone="critical"
                                  onClick={() => handleRescrapeToggle(product.id, true)}
                                  disabled={!isOn || undefined}
                                >
                                  Turn off
                                </s-button>
                              </>
                            ) : (
                              <>
                                <s-badge tone="subdued">OFF</s-badge>
                                <s-button
                                  size="slim"
                                  onClick={() => handleRescrapeToggle(product.id, false)}
                                  disabled={!isOn || undefined}
                                >
                                  Turn on
                                </s-button>
                              </>
                            )}
                          </>
                        );
                      })()}
                      <s-button
                        variant="plain"
                        size="slim"
                        id={`expand-${product.id}`}
                        onClick={() => toggleExpand(product.id)}
                        aria-label={isExpanded ? "Collapse details" : "Expand details"}
                      >
                        {isExpanded ? "▾ Hide" : "▸ Details"}
                      </s-button>
                    </s-stack>

                    {isExpanded && (
                      <s-box
                        padding="base"
                        borderWidth="base"
                        borderRadius="base"
                        background="subdued"
                      >
                        <s-stack direction="block" gap="base">
                          <s-stack direction="inline" gap="loose" align="center">
                            <s-text tone="subdued">Verified competitors:</s-text>
                            <s-badge>{product.matchCount}</s-badge>
                            <s-text tone="subdued">Candidates seen:</s-text>
                            <s-badge tone="info">{product.candidateCount}</s-badge>
                            {product.lastDiscoveryAt && (
                              <s-text tone="subdued">
                                Last run {new Date(product.lastDiscoveryAt).toLocaleString()}
                              </s-text>
                            )}
                          </s-stack>

                          <s-divider />

                          <s-text emphasis="bold">Sync to Shopify</s-text>
                          <s-stack direction="inline" gap="loose">
                            <s-checkbox
                              id={`price-${product.id}`}
                              label="Price"
                              checked={local.syncPrice || undefined}
                              disabled={!isOn || undefined}
                              onChange={() =>
                                handleFieldChange(product.id, "syncPrice", local.syncPrice)
                              }
                            />
                            <s-checkbox
                              id={`description-${product.id}`}
                              label="Description"
                              checked={local.syncDescription || undefined}
                              disabled={!isOn || undefined}
                              onChange={() =>
                                handleFieldChange(product.id, "syncDescription", local.syncDescription)
                              }
                            />
                            <s-checkbox
                              id={`title-${product.id}`}
                              label="Title"
                              checked={local.syncTitle || undefined}
                              disabled={!isOn || undefined}
                              onChange={() =>
                                handleFieldChange(product.id, "syncTitle", local.syncTitle)
                              }
                            />
                          </s-stack>

                          <s-divider />

                          <s-text emphasis="bold">Search query (used to find competitors)</s-text>
                          <s-text-field
                            label="Search query"
                            placeholder={product.searchQuery || "e.g. nike air max 90 blue mens"}
                            value={local.searchQueryOverride || product.searchQuery || ""}
                            helpText={
                              product.searchQuery && !local.searchQueryOverride
                                ? `Generated by AI from the product details. Edit to refine.`
                                : `Edit to override the generated query, or leave it as-is.`
                            }
                            onInput={(e) => {
                              const v = e.currentTarget.value;
                              // Only treat user typing as override; if they
                              // wipe it back to the generated value, store
                              // empty so the backend keeps the regen behavior.
                              setOverrideField(
                                product.id,
                                "searchQueryOverride",
                                v === (product.searchQuery || "") ? "" : v,
                              );
                            }}
                          />

                          <s-text emphasis="bold">Number of competitor products to fetch</s-text>
                          <s-text tone="subdued">
                            How many product links discovery should pull per run (1–50).
                          </s-text>
                          <s-text-field
                            label="Number of products"
                            type="number"
                            min="1"
                            max="50"
                            value={String(local.discoveryNumResults ?? "")}
                            onInput={(e) =>
                              setOverrideField(product.id, "discoveryNumResults", e.currentTarget.value)
                            }
                          />

                          <s-text-field
                            label="Number of products to scrape from listing page"
                            type="number"
                            min="1"
                            max="50"
                            value={String(local.listingExpansionCap ?? "")}
                            onInput={(e) =>
                              setOverrideField(product.id, "listingExpansionCap", e.currentTarget.value)
                            }
                            helpText="When a discovered URL is a search/category page, expand this many product cards from it (1–50)."
                          />

                          <s-text emphasis="bold">Pricing tier</s-text>
                          <s-text tone="subdued">
                            Budget undercuts competitors more aggressively;
                            Premium prices slightly above the weighted median.
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

                          <s-text emphasis="bold">Hard price bounds (override the lifetime cap)</s-text>
                          {local.basePrice && (
                            <s-text tone="subdued">
                              Base price snapshot: ₹{Number(local.basePrice).toFixed(2)}.
                              Lifetime cap defaults to ±25% from this anchor.
                            </s-text>
                          )}
                          <s-stack direction="inline" gap="base">
                            <s-text-field
                              label="Minimum price"
                              type="number"
                              placeholder="0.00"
                              value={local.minPriceOverride ?? ""}
                              onInput={(e) =>
                                setOverrideField(product.id, "minPriceOverride", e.currentTarget.value)
                              }
                            />
                            <s-text-field
                              label="Maximum price"
                              type="number"
                              placeholder="0.00"
                              value={local.maxPriceOverride ?? ""}
                              onInput={(e) =>
                                setOverrideField(product.id, "maxPriceOverride", e.currentTarget.value)
                              }
                            />
                          </s-stack>

                          <s-text emphasis="bold">Legacy floor / ceiling (deprecated)</s-text>
                          <s-stack direction="inline" gap="base">
                            <s-text-field
                              label="Floor"
                              type="number"
                              placeholder="0.00"
                              value={local.floorPrice ?? ""}
                              onInput={(e) =>
                                setOverrideField(product.id, "floorPrice", e.currentTarget.value)
                              }
                            />
                            <s-text-field
                              label="Ceiling"
                              type="number"
                              placeholder="0.00"
                              value={local.ceilingPrice ?? ""}
                              onInput={(e) =>
                                setOverrideField(product.id, "ceilingPrice", e.currentTarget.value)
                              }
                            />
                          </s-stack>

                          <s-text emphasis="bold">Rescrape frequency</s-text>
                          <s-text tone="subdued">
                            Required for dynamic pricing to keep prices fresh. Empty falls back to shop default.
                          </s-text>
                          <s-stack direction="inline" gap="base">
                            <s-text-field
                              label="Every"
                              type="number"
                              placeholder="e.g. 15"
                              value={local.frequencyInterval ?? ""}
                              onInput={(e) =>
                                setOverrideField(product.id, "frequencyInterval", e.currentTarget.value)
                              }
                            />
                            <s-select
                              label="Unit"
                              value={local.frequencyUnit || ""}
                              onChange={(e) =>
                                setOverrideField(product.id, "frequencyUnit", e.currentTarget.value)
                              }
                            >
                              <s-option value="">(shop default)</s-option>
                              {FREQ_UNITS.map((u) => (
                                <s-option key={u.value} value={u.value}>{u.label}</s-option>
                              ))}
                            </s-select>
                          </s-stack>

                          <s-stack direction="inline" gap="base" align="center">
                            <s-button
                              variant="primary"
                              onClick={() => submitOverrides(product.id, { enable: !isOn })}
                            >
                              {isOn ? "Save changes" : "Save & start dynamic pricing"}
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
                    )}
                  </s-stack>
                </s-resource-item>
              );
            })}
          </s-resource-list>
        )}
      </s-section>
    </s-page>
  );
}

export const headers = (headersArgs) => {
  return boundary.headers(headersArgs);
};
