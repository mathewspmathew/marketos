import { useState, useMemo } from "react";
import { useFetcher, useLoaderData } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

// ─── Loader ──────────────────────────────────────────────────────────────────
export const loader = async ({ request }) => {
  const { admin, session } = await authenticate.admin(request);
  const shop = session.shop;

  // First-time sync: if we have no products for this shop, pull from Shopify GraphQL
  const count = await db.product.count({ where: { shop } });
  if (count === 0) {
    await syncProductsFromShopify(admin, shop);
  }

  const products = await db.product.findMany({
    where: { shop },
    orderBy: { updatedAt: "desc" },
  });

  return { products };
};

// ─── GraphQL full sync helper ─────────────────────────────────────────────────
async function syncProductsFromShopify(admin, shop) {
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
              status
              tags
              featuredImage { url }
              variants(first: 1) {
                edges {
                  node {
                    price
                    compareAtPrice
                  }
                }
              }
            }
          }
        }
      }
    `;
    const response = await admin.graphql(query, {
      variables: { cursor },
    });
    const json = await response.json();
    const { edges, pageInfo } = json.data.products;

    for (const { node } of edges) {
      const variant = node.variants?.edges?.[0]?.node ?? {};
      await db.product.upsert({
        where: { id: node.id },
        update: {
          title: node.title,
          description: node.descriptionHtml ?? "",
          price: variant.price ?? "0.00",
          compareAtPrice: variant.compareAtPrice ?? null,
          tags: JSON.stringify(node.tags ?? []),
          productType: node.productType ?? "",
          imageUrl: node.featuredImage?.url ?? null,
          status: node.status ?? "ACTIVE",
          vectorized: false, // Reset on update
        },
        create: {
          id: node.id,
          shop,
          title: node.title,
          description: node.descriptionHtml ?? "",
          price: variant.price ?? "0.00",
          compareAtPrice: variant.compareAtPrice ?? null,
          tags: JSON.stringify(node.tags ?? []),
          productType: node.productType ?? "",
          imageUrl: node.featuredImage?.url ?? null,
          status: node.status ?? "ACTIVE",
          source: "INTERNAL",
          vectorized: false,
        },
      });
    }

    hasNextPage = pageInfo.hasNextPage;
    cursor = pageInfo.endCursor;
  }
}

// ─── Action (persist toggle + checkbox settings) ──────────────────────────────
export const action = async ({ request }) => {
  await authenticate.admin(request);
  const formData = await request.formData();
  const intent = formData.get("intent");
  const productId = formData.get("productId");

  if (intent === "toggleDynamic") {
    const enabled = formData.get("enabled") === "true";
    await db.product.update({
      where: { id: productId },
      data: { dynamicChangeEnabled: enabled },
    });
  } else if (intent === "updateFields") {
    await db.product.update({
      where: { id: productId },
      data: {
        fieldPrice: formData.get("fieldPrice") === "true",
        fieldDescription: formData.get("fieldDescription") === "true",
        fieldTitle: formData.get("fieldTitle") === "true",
      },
    });
  }

  return null;
};

// ─── UI ───────────────────────────────────────────────────────────────────────
export default function HomePage() {
  const { products } = useLoaderData();
  const fetcher = useFetcher();

  // Local filter state
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedTag, setSelectedTag] = useState("all");
  const [selectedCategory, setSelectedCategory] = useState("all");

  // Which product row is expanded (accordion — one at a time)
  const [expandedId, setExpandedId] = useState(null);

  // Optimistic local copies of DB state (keyed by product id)
  const [localState, setLocalState] = useState(() => {
    const map = {};
    for (const p of products) {
      map[p.id] = {
        dynamicChangeEnabled: p.dynamicChangeEnabled,
        fieldPrice: p.fieldPrice,
        fieldDescription: p.fieldDescription,
        fieldTitle: p.fieldTitle,
      };
    }
    return map;
  });

  // ── Derived filter options ──────────────────────────────────────────────────
  const allTags = useMemo(() => {
    const tagSet = new Set();
    for (const p of products) {
      try {
        const parsed = JSON.parse(p.tags);
        if (Array.isArray(parsed)) parsed.forEach((t) => tagSet.add(t));
      } catch (_e) { /* invalid JSON — skip */ }
    }
    return [...tagSet].sort();
  }, [products]);

  const allCategories = useMemo(() => {
    const catSet = new Set(products.map((p) => p.productType).filter(Boolean));
    return [...catSet].sort();
  }, [products]);

  // ── Filtered product list ───────────────────────────────────────────────────
  const filteredProducts = useMemo(() => {
    return products.filter((p) => {
      const matchesSearch = p.title
        .toLowerCase()
        .includes(searchQuery.toLowerCase());
      const productTags = (() => {
        try { return JSON.parse(p.tags); } catch { return []; }
      })();
      const matchesTag =
        selectedTag === "all" || productTags.includes(selectedTag);
      const matchesCategory =
        selectedCategory === "all" || p.productType === selectedCategory;
      return matchesSearch && matchesTag && matchesCategory;
    });
  }, [products, searchQuery, selectedTag, selectedCategory]);

  // ── Helpers ─────────────────────────────────────────────────────────────────
  const getLocal = (id) =>
    localState[id] ?? {
      dynamicChangeEnabled: false,
      fieldPrice: true,
      fieldDescription: false,
      fieldTitle: false,
    };

  const handleToggle = (productId, currentValue) => {
    const newValue = !currentValue;
    setLocalState((prev) => ({
      ...prev,
      [productId]: { ...prev[productId], dynamicChangeEnabled: newValue },
    }));
    fetcher.submit(
      { intent: "toggleDynamic", productId, enabled: String(newValue) },
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
        fieldPrice: String(updated.fieldPrice),
        fieldDescription: String(updated.fieldDescription),
        fieldTitle: String(updated.fieldTitle),
      },
      { method: "POST" },
    );
  };

  const toggleExpand = (id) =>
    setExpandedId((prev) => (prev === id ? null : id));

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <s-page heading="Dynamic Pricing — Home">
      {/* ── Filter bar ── */}
      <s-section>
        <s-stack direction="inline" gap="base" wrap>
          {/* onInput fires on every keystroke; onChange only fires on blur */}
          <s-text-field
            label="Search products"
            placeholder="Search by name…"
            value={searchQuery}
            onInput={(e) => setSearchQuery(e.currentTarget.value)}
            clearButton
            onClearButtonClick={() => setSearchQuery("")}
          />
          {/* s-select requires s-option children, not native <option> */}
          <s-select
            label="Tag"
            value={selectedTag}
            onChange={(e) => setSelectedTag(e.currentTarget.value)}
          >
            <s-option value="all">All Tags</s-option>
            {allTags.map((tag) => (
              <s-option key={tag} value={tag}>
                {tag}
              </s-option>
            ))}
          </s-select>
          <s-select
            label="Category"
            value={selectedCategory}
            onChange={(e) => setSelectedCategory(e.currentTarget.value)}
          >
            <s-option value="all">All Categories</s-option>
            {allCategories.map((cat) => (
              <s-option key={cat} value={cat}>
                {cat}
              </s-option>
            ))}
          </s-select>
        </s-stack>
      </s-section>

      {/* ── Product list ── */}
      <s-section>
        {filteredProducts.length === 0 ? (
          <s-paragraph>No products match your filters.</s-paragraph>
        ) : (
          <s-resource-list>
            {filteredProducts.map((product) => {
              const local = getLocal(product.id);
              const isOn = local.dynamicChangeEnabled;
              const isExpanded = expandedId === product.id;
              const productTags = (() => {
                try { return JSON.parse(product.tags); } catch (_e) { return []; }
              })();

              return (
                <s-resource-item key={product.id} id={product.id}>
                  {/* ── Product thumbnail ── */}
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

                  {/* ── Main content ── */}
                  <s-stack direction="block" gap="tight">
                    {/* Row 1: title + price */}
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

                    {/* Row 2: tags */}
                    {productTags.length > 0 && (
                      <s-stack direction="inline" gap="tight">
                        {productTags.slice(0, 5).map((tag) => (
                          <s-badge key={tag} tone="info">
                            {tag}
                          </s-badge>
                        ))}
                      </s-stack>
                    )}

                    {/* Row 3: Dynamic Change toggle + expand arrow */}
                    <s-stack direction="inline" gap="base" align="center">
                      <s-text>Dynamic Change</s-text>
                      <s-toggle
                        id={`toggle-${product.id}`}
                        checked={isOn || undefined}
                        onClick={() => handleToggle(product.id, isOn)}
                      />
                      <s-button
                        variant="plain"
                        size="slim"
                        id={`expand-${product.id}`}
                        onClick={() => toggleExpand(product.id)}
                        aria-label={isExpanded ? "Collapse details" : "Expand details"}
                      >
                        {isExpanded ? "▾" : "▸"}
                      </s-button>
                    </s-stack>

                    {/* ── Expandable detail panel ── */}
                    {isExpanded && (
                      <s-box
                        padding="base"
                        borderWidth="base"
                        borderRadius="base"
                        background="subdued"
                      >
                        <s-stack direction="block" gap="tight">
                          <s-text emphasis="bold">
                            Which fields should update dynamically?
                          </s-text>
                          <s-stack direction="inline" gap="loose">
                            <s-checkbox
                              id={`price-${product.id}`}
                              label="Price"
                              checked={local.fieldPrice || undefined}
                              disabled={!isOn || undefined}
                              onChange={() =>
                                handleFieldChange(product.id, "fieldPrice", local.fieldPrice)
                              }
                            />
                            <s-checkbox
                              id={`description-${product.id}`}
                              label="Description"
                              checked={local.fieldDescription || undefined}
                              disabled={!isOn || undefined}
                              onChange={() =>
                                handleFieldChange(
                                  product.id,
                                  "fieldDescription",
                                  local.fieldDescription,
                                )
                              }
                            />
                            <s-checkbox
                              id={`title-${product.id}`}
                              label="Title"
                              checked={local.fieldTitle || undefined}
                              disabled={!isOn || undefined}
                              onChange={() =>
                                handleFieldChange(product.id, "fieldTitle", local.fieldTitle)
                              }
                            />
                          </s-stack>
                          {!isOn && (
                            <s-text tone="subdued">
                              Enable Dynamic Change to configure fields.
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
