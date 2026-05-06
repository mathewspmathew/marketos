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
    include: { variants: { take: 1 } },
    orderBy: { updatedAt: "desc" },
  });

  const flattened = products.map((p) => ({
    ...p,
    price: p.variants[0]?.currentPrice?.toString() ?? "0.00",
    compareAtPrice: p.variants[0]?.compareAtPrice?.toString() ?? null,
  }));

  return { products: flattened };
};

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
    };

  const handleToggle = (productId, currentValue) => {
    const newValue = !currentValue;
    setLocalState((prev) => ({
      ...prev,
      [productId]: { ...prev[productId], dynamicPricingEnabled: newValue },
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

                    {isExpanded && (
                      <s-box
                        padding="base"
                        borderWidth="base"
                        borderRadius="base"
                        background="subdued"
                      >
                        <s-stack direction="block" gap="tight">
                          <s-text emphasis="bold">
                            Which fields should sync dynamically?
                          </s-text>
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
                          {!isOn && (
                            <s-text tone="subdued">
                              Enable Dynamic Pricing to configure sync fields.
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
