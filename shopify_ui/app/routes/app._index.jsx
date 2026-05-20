import { useState, useMemo } from "react";
import { useFetcher, useLoaderData } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

// ─── Loader ──────────────────────────────────────────────────────────────────
export const loader = async ({ request }) => {
  const { admin, session } = await authenticate.admin(request);
  const shopDomain = session.shop;

  // Ensure ShopifyUser row exists for this shop
  await db.shopifyUser.upsert({
    where: { shopDomain },
    update: {},
    create: { shopDomain },
  });

  // First-time sync: pull products from Shopify GraphQL if none stored yet
  const count = await db.shopifyProduct.count({ where: { shopDomain } });
  if (count === 0) {
    await syncProductsFromShopify(admin, shopDomain);
  }

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
    frequencyInterval: p.frequencyInterval ?? "",
    frequencyUnit: p.frequencyUnit ?? "",
    discoveryNumResults: p.discoveryNumResults ?? 10,
    lastDiscoveryAt: p.lastDiscoveryAt ? p.lastDiscoveryAt.toISOString() : null,
    matchCount: p._count.productLevelMatches,
    candidateCount: p._count.competitorCandidates,
  }));

  return { products: flattened };
};

// Canonical frequency-unit dropdown — keep in sync with
// services/common/frequency.py::CANONICAL_UNITS / UNIT_LABELS.
const FREQ_UNITS = [
  { value: "never",  label: "Never (one-time discovery)" },
  { value: "minute", label: "Minutes" },
  { value: "hour",   label: "Hours"   },
  { value: "day",    label: "Days"    },
];

// ─── GraphQL full sync helper ─────────────────────────────────────────────────
async function syncProductsFromShopify(admin, shopDomain) {
  let hasNextPage = true;
  let cursor = null;

  while (hasNextPage) {
    const query = `#graphql
      query getProducts($cursor: String) {
        products(first: 50, after: $cursor) {
          pageInfo { hasNextPage endCursor }
          edges {
            node {
              id
              title
              descriptionHtml
              productType
              handle
              status
              tags
              featuredImage { url }
              variants(first: 10) {
                edges {
                  node {
                    id
                    title
                    price
                    compareAtPrice
                    sku
                    barcode
                    image { url }
                    selectedOptions { name value }
                  }
                }
              }
            }
          }
        }
      }
    `;
    const response = await admin.graphql(query, { variables: { cursor } });
    const json = await response.json();
    const { edges, pageInfo } = json.data.products;

    for (const { node } of edges) {
      await db.shopifyProduct.upsert({
        where: { id: node.id },
        update: {
          title: node.title,
          description: node.descriptionHtml ?? "",
          tags: node.tags ?? [],
          productType: node.productType ?? "",
          handle: node.handle ?? null,
          imageUrl: node.featuredImage?.url ?? null,
          status: node.status ?? "ACTIVE",
        },
        create: {
          id: node.id,
          shopDomain,
          title: node.title,
          description: node.descriptionHtml ?? "",
          tags: node.tags ?? [],
          productType: node.productType ?? "",
          handle: node.handle ?? null,
          imageUrl: node.featuredImage?.url ?? null,
          status: node.status ?? "ACTIVE",
        },
      });

      for (const { node: vNode } of node.variants.edges) {
        const options = {};
        vNode.selectedOptions.forEach((opt) => { options[opt.name] = opt.value; });

        await db.shopifyVariant.upsert({
          where: { id: vNode.id },
          update: {
            title: vNode.title,
            currentPrice: vNode.price,
            compareAtPrice: vNode.compareAtPrice ?? null,
            sku: vNode.sku,
            barcode: vNode.barcode,
            imageUrl: vNode.image?.url ?? null,
            options,
          },
          create: {
            id: vNode.id,
            productId: node.id,
            title: vNode.title,
            currentPrice: vNode.price,
            compareAtPrice: vNode.compareAtPrice ?? null,
            sku: vNode.sku,
            barcode: vNode.barcode,
            imageUrl: vNode.image?.url ?? null,
            options,
          },
        });
      }
    }

    hasNextPage = pageInfo.hasNextPage;
    cursor = pageInfo.endCursor;
  }
}

// ─── Action ───────────────────────────────────────────────────────────────────
export const action = async ({ request }) => {
  await authenticate.admin(request);
  const formData = await request.formData();
  const intent = formData.get("intent");
  const productId = formData.get("productId");

  if (intent === "toggleDynamic") {
    // Disable-only path. Enabling now goes through "saveAndEnable" so the
    // overrides are committed atomically with the toggle flip.
    const enabled = formData.get("enabled") === "true";
    await db.shopifyProduct.update({
      where: { id: productId },
      data: { dynamicPricingEnabled: enabled },
    });
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

    if (rawNumResults !== null && rawNumResults !== "") {
      const n = parseInt(rawNumResults, 10);
      if (Number.isFinite(n) && n > 0) data.discoveryNumResults = Math.min(n, 50);
    }

    if (intent === "saveAndEnable") {
      data.dynamicPricingEnabled = true;
    }

    await db.shopifyProduct.update({
      where: { id: productId },
      data,
    });
  }

  return null;
};

// ─── UI ───────────────────────────────────────────────────────────────────────
export default function HomePage() {
  const { products } = useLoaderData();
  const fetcher = useFetcher();

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
        frequencyInterval: p.frequencyInterval === "" ? "" : String(p.frequencyInterval),
        frequencyUnit: p.frequencyUnit,
        discoveryNumResults: p.discoveryNumResults ?? 10,
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
      frequencyInterval: "",
      frequencyUnit: "",
      discoveryNumResults: 10,
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
        frequencyInterval:   local.frequencyInterval ?? "",
        frequencyUnit:       local.frequencyUnit ?? "",
        discoveryNumResults: String(local.discoveryNumResults ?? 10),
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

  return (
    <s-page
      heading="Dynamic Pricing"
      subheading={`${filteredProducts.length} of ${products.length} product${products.length === 1 ? "" : "s"}`}
    >
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

                          <s-text emphasis="bold">Price bounds (applied to all variants)</s-text>
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
                            <s-link href={`/app/history/${encodeURIComponent(product.id)}`}>
                              Price history
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
