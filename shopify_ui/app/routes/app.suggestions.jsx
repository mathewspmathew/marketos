import { useMemo, useState } from "react";
import { useFetcher, useLoaderData, useRouteError } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

const PYTHON_API_URL = process.env.PYTHON_API_URL ?? "http://localhost:8000";

// ─── Loader ──────────────────────────────────────────────────────────────────
export const loader = async ({ request }) => {
  const url = new URL(request.url);
  const showSkipped = url.searchParams.get("showSkipped") === "1";

  const { admin, session } = await authenticate.admin(request);
  const shop = session.shop;

  // Only products that have ≥1 variant with a VariantPriceSuggestion (i.e. a
  // qualifying competitor match was found by the worker).
  const products = await db.shopifyProduct.findMany({
    where: {
      shopDomain: shop,
      variants: { some: { priceSuggestion: { isNot: null } } },
      ...(showSkipped ? {} : { suggestion: { status: { not: "SKIPPED" } } }),
    },
    include: {
      suggestion: true,
      variants: {
        where: { priceSuggestion: { isNot: null } },
        include: { priceSuggestion: true },
      },
    },
    orderBy: { updatedAt: "desc" },
  });

  // Hide variants whose own price-suggestion is SKIPPED unless toggled.
  // Also stringify all Decimal fields here — Prisma Decimal objects lose
  // their prototype across the loader serialization boundary, so any
  // Number(d) call in the component would return NaN.
  const dec = (d) => (d == null ? null : d.toString());
  const cleaned = products
    .map((p) => ({
      ...p,
      variants: (showSkipped
        ? p.variants
        : p.variants.filter((v) => v.priceSuggestion?.status !== "SKIPPED")
      ).map((v) => ({
        ...v,
        currentPrice: dec(v.currentPrice),
        compareAtPrice: dec(v.compareAtPrice),
        priceSuggestion: v.priceSuggestion
          ? {
              ...v.priceSuggestion,
              competitorMin: dec(v.priceSuggestion.competitorMin),
              competitorMedian: dec(v.priceSuggestion.competitorMedian),
              competitorMax: dec(v.priceSuggestion.competitorMax),
              chosenPrice: dec(v.priceSuggestion.chosenPrice),
              appliedPrice: dec(v.priceSuggestion.appliedPrice),
            }
          : null,
      })),
      suggestion: p.suggestion
        ? { ...p.suggestion, avgMatchScore: dec(p.suggestion.avgMatchScore) }
        : null,
    }))
    .filter((p) => p.variants.length > 0);

  // Backfill missing handles from Shopify so the storefront link works.
  const missingHandle = cleaned.filter((p) => !p.handle).map((p) => p.id);
  if (missingHandle.length > 0) {
    try {
      const resp = await admin.graphql(
        `#graphql
        query BackfillHandles($ids: [ID!]!) {
          nodes(ids: $ids) { ... on Product { id handle } }
        }`,
        { variables: { ids: missingHandle } },
      );
      const data = await resp.json();
      const nodes = data?.data?.nodes ?? [];
      for (const n of nodes) {
        if (n?.id && n?.handle) {
          await db.shopifyProduct.update({
            where: { id: n.id },
            data: { handle: n.handle },
          });
          const p = cleaned.find((x) => x.id === n.id);
          if (p) p.handle = n.handle;
        }
      }
    } catch (err) {
      console.error("[suggestions] handle backfill failed:", err);
    }
  }

  return { products: cleaned, shop, showSkipped };
};

// ─── Action ──────────────────────────────────────────────────────────────────
export const action = async ({ request }) => {
  const { admin, session } = await authenticate.admin(request);
  const shop = session.shop;
  const formData = await request.formData();
  const intent = formData.get("intent");

  if (intent === "regenerateAll") {
    try {
      await fetch(
        `${PYTHON_API_URL}/internal/suggestion/regenerate?shop_domain=${encodeURIComponent(shop)}&scope=all`,
        { method: "POST" },
      );
    } catch (err) {
      console.error("[suggestions] regenerate trigger failed:", err);
      return { ok: false, error: "Failed to enqueue regeneration." };
    }
    return { ok: true, queued: true };
  }

  if (intent === "regenerateProduct") {
    const productId = formData.get("productId");
    const owned = await db.shopifyProduct.findFirst({
      where: { id: productId, shopDomain: shop },
      select: { id: true },
    });
    if (!owned) {
      return { ok: false, error: "Forbidden" };
    }
    try {
      await fetch(
        `${PYTHON_API_URL}/internal/suggestion/regenerate-product?shop_domain=${encodeURIComponent(shop)}&product_id=${encodeURIComponent(productId)}`,
        { method: "POST" },
      );
    } catch (err) {
      return { ok: false, error: "Failed to enqueue regeneration." };
    }
    return { ok: true };
  }

  // Tenant-scoped helpers: ensure the product/variant belongs to this shop
  // before any mutation. Returns null if not owned.
  const assertProductInShop = async (productId) => {
    const row = await db.shopifyProduct.findFirst({
      where: { id: productId, shopDomain: shop },
      select: { id: true },
    });
    return row?.id ?? null;
  };
  const assertVariantInShop = async (variantId) => {
    const row = await db.shopifyVariant.findFirst({
      where: { id: variantId, product: { shopDomain: shop } },
      select: { id: true },
    });
    return row?.id ?? null;
  };

  if (intent === "saveProductEdits") {
    const productId = formData.get("productId");
    if (!(await assertProductInShop(productId))) {
      return { ok: false, error: "Forbidden" };
    }
    const editedTitle = formData.get("editedTitle") || null;
    const editedDescriptionHtml = formData.get("editedDescriptionHtml") || null;

    const existing = await db.productSuggestion.findUnique({
      where: { shopifyProductId: productId },
      select: { status: true },
    });
    await db.productSuggestion.update({
      where: { shopifyProductId: productId },
      data: {
        editedTitle,
        editedDescriptionHtml,
        // Preserve APPLIED; otherwise mark SHOWED on edit
        status: existing?.status === "APPLIED" ? "APPLIED" : "SHOWED",
      },
    });
    return { ok: true };
  }

  if (intent === "saveVariantPrice") {
    const variantId = formData.get("variantId");
    if (!(await assertVariantInShop(variantId))) {
      return { ok: false, error: "Forbidden" };
    }
    const chosenPrice = formData.get("chosenPrice");
    const existing = await db.variantPriceSuggestion.findUnique({
      where: { shopifyVariantId: variantId },
      select: { status: true },
    });
    await db.variantPriceSuggestion.update({
      where: { shopifyVariantId: variantId },
      data: {
        chosenPrice: chosenPrice ? chosenPrice : null,
        status: existing?.status === "APPLIED" ? "APPLIED" : "SHOWED",
      },
    });
    return { ok: true };
  }

  if (intent === "skipProduct") {
    const productId = formData.get("productId");
    if (!(await assertProductInShop(productId))) {
      return { ok: false, error: "Forbidden" };
    }
    await db.productSuggestion.update({
      where: { shopifyProductId: productId },
      data: { status: "SKIPPED" },
    });
    return { ok: true };
  }

  if (intent === "skipVariant") {
    const variantId = formData.get("variantId");
    if (!(await assertVariantInShop(variantId))) {
      return { ok: false, error: "Forbidden" };
    }
    await db.variantPriceSuggestion.update({
      where: { shopifyVariantId: variantId },
      data: { status: "SKIPPED" },
    });
    return { ok: true };
  }

  // ── APPLY: persist latest edits, then write to Shopify + mirror to our DB ──
  if (intent === "applyProduct") {
    const productId = formData.get("productId");
    if (!(await assertProductInShop(productId))) {
      return { ok: false, error: "Forbidden" };
    }

    // Persist any final edits the form is carrying so the source of truth
    // for what we apply is the DB row (not the JS form state).
    const editedTitle = formData.get("editedTitle");
    const editedDescriptionHtml = formData.get("editedDescriptionHtml");
    if (editedTitle != null || editedDescriptionHtml != null) {
      await db.productSuggestion.update({
        where: { shopifyProductId: productId },
        data: {
          ...(editedTitle != null ? { editedTitle: editedTitle || null } : {}),
          ...(editedDescriptionHtml != null
            ? { editedDescriptionHtml: editedDescriptionHtml || null }
            : {}),
        },
      });
    }

    // Persist all variant prices carried by the form before reading them back.
    // Form field naming convention: variantPrice__<variantId> = "<price>"
    for (const [key, value] of formData.entries()) {
      if (!key.startsWith("variantPrice__")) continue;
      const variantId = key.slice("variantPrice__".length);
      if (!(await assertVariantInShop(variantId))) continue;
      const trimmed = String(value ?? "").trim();
      if (!trimmed) continue;
      await db.variantPriceSuggestion.update({
        where: { shopifyVariantId: variantId },
        data: { chosenPrice: trimmed },
      });
    }

    const ps = await db.productSuggestion.findUnique({
      where: { shopifyProductId: productId },
    });
    const variants = await db.shopifyVariant.findMany({
      where: { productId, priceSuggestion: { isNot: null } },
      include: { priceSuggestion: true },
    });

    const finalTitle =
      ps?.editedTitle ?? ps?.suggestedTitle ?? null;
    const finalDescHtml =
      ps?.editedDescriptionHtml ?? ps?.suggestedDescriptionHtml ?? null;

    const variantUpdates = variants
      .filter(
        (v) =>
          v.priceSuggestion?.chosenPrice != null &&
          v.priceSuggestion?.status !== "SKIPPED",
      )
      .map((v) => ({
        id: v.id,
        price: String(v.priceSuggestion.chosenPrice),
      }));

    const errors = [];
    let appliedTitle = null;
    let appliedDescHtml = null;

    // 1. productUpdate (title + descriptionHtml) — only if either changed
    if (finalTitle != null || finalDescHtml != null) {
      const productInput = { id: productId };
      if (finalTitle != null) productInput.title = finalTitle;
      if (finalDescHtml != null) productInput.descriptionHtml = finalDescHtml;

      const resp = await admin.graphql(
        `#graphql
        mutation ProductApply($input: ProductInput!) {
          productUpdate(input: $input) {
            product { id title descriptionHtml }
            userErrors { field message }
          }
        }`,
        { variables: { input: productInput } },
      );
      const data = await resp.json();
      const userErrors = data?.data?.productUpdate?.userErrors ?? [];
      if (userErrors.length) {
        errors.push(`product: ${userErrors.map((e) => e.message).join("; ")}`);
      } else {
        appliedTitle = data?.data?.productUpdate?.product?.title ?? null;
        appliedDescHtml =
          data?.data?.productUpdate?.product?.descriptionHtml ?? null;
      }
    }

    // 2. productVariantsBulkUpdate (prices)
    let appliedVariantPrices = [];
    if (variantUpdates.length > 0) {
      const resp = await admin.graphql(
        `#graphql
        mutation VariantsApply($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id price }
            userErrors { field message }
          }
        }`,
        {
          variables: {
            productId,
            variants: variantUpdates,
          },
        },
      );
      const data = await resp.json();
      const userErrors =
        data?.data?.productVariantsBulkUpdate?.userErrors ?? [];
      if (userErrors.length) {
        errors.push(`variants: ${userErrors.map((e) => e.message).join("; ")}`);
      } else {
        appliedVariantPrices =
          data?.data?.productVariantsBulkUpdate?.productVariants ?? [];
      }
    }

    // 3. Mirror to DB — record what actually got applied
    const now = new Date();
    if (ps && (appliedTitle || appliedDescHtml) && errors.length === 0) {
      await db.productSuggestion.update({
        where: { shopifyProductId: productId },
        data: {
          appliedTitle,
          appliedDescriptionHtml: appliedDescHtml,
          status: "APPLIED",
          appliedAt: now,
        },
      });
      // Update the canonical ShopifyProduct row too so loader reflects new state
      await db.shopifyProduct.update({
        where: { id: productId },
        data: {
          ...(appliedTitle ? { title: appliedTitle } : {}),
          ...(appliedDescHtml ? { description: appliedDescHtml } : {}),
          syncedAt: now,
        },
      });
    }

    if (appliedVariantPrices.length > 0 && errors.length === 0) {
      for (const vp of appliedVariantPrices) {
        await db.variantPriceSuggestion.update({
          where: { shopifyVariantId: vp.id },
          data: {
            appliedPrice: vp.price,
            status: "APPLIED",
            appliedAt: now,
          },
        });
        await db.shopifyVariant.update({
          where: { id: vp.id },
          data: { currentPrice: vp.price },
        });
      }
    }

    if (errors.length > 0) {
      return { ok: false, error: errors.join(" | ") };
    }
    return { ok: true, applied: true };
  }

  return { ok: false, error: `Unknown intent: ${intent}` };
};

// ─── Helpers ─────────────────────────────────────────────────────────────────
function formatINR(n) {
  if (n == null) return "—";
  return `₹${Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

// Storefront URL for the product page
function productStoreUrl(shop, handle) {
  if (!shop || !handle) return null;
  return `https://${shop}/products/${handle}`;
}

function priceRange(variants) {
  const prices = variants
    .map((v) => Number(v.currentPrice))
    .filter((n) => !Number.isNaN(n));
  if (prices.length === 0) return null;
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  return min === max
    ? formatINR(min)
    : `${formatINR(min)} – ${formatINR(max)}`;
}

function statusTone(s) {
  if (s === "APPLIED") return "success";
  if (s === "FIRST_TIME") return "info";
  if (s === "SKIPPED") return "subdued";
  return "info";
}

// ─── UI ──────────────────────────────────────────────────────────────────────
export default function SuggestionsPage() {
  const { products, showSkipped, shop } = useLoaderData();
  const fetcher = useFetcher();
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return products;
    return products.filter((p) => p.title.toLowerCase().includes(q));
  }, [products, query]);

  const handleRegenerateAll = () => {
    fetcher.submit({ intent: "regenerateAll" }, { method: "POST" });
  };

  const toggleSkipped = () => {
    const next = new URL(window.location.href);
    if (showSkipped) next.searchParams.delete("showSkipped");
    else next.searchParams.set("showSkipped", "1");
    window.location.href = next.toString();
  };

  return (
    <s-page
      heading="Product Suggestions"
      subheading="LLM-generated title & description plus competitor price ranges. Edit, then Apply to push back to Shopify."
    >
      <s-stack direction="block" gap="loose">
        <s-section>
          <s-stack direction="inline" gap="base" align="center">
            <s-text-field
              label="Search"
              placeholder="Search products…"
              value={query}
              onInput={(e) => setQuery(e.currentTarget.value)}
              clearButton
              onClearButtonClick={() => setQuery("")}
            />
            <s-spacer />
            <s-button variant="secondary" onClick={toggleSkipped}>
              {showSkipped ? "Hide skipped" : "Show skipped"}
            </s-button>
            <s-button
              variant="primary"
              onClick={handleRegenerateAll}
              disabled={
                fetcher.state === "submitting" &&
                fetcher.formData?.get("intent") === "regenerateAll"
              }
            >
              {fetcher.state === "submitting" &&
              fetcher.formData?.get("intent") === "regenerateAll"
                ? "Queuing…"
                : "Re-suggest"}
            </s-button>
          </s-stack>
          <s-paragraph tone="subdued">
            Re-suggest enqueues a background job. Refresh this page after a
            minute to see new suggestions.
          </s-paragraph>
        </s-section>

        {filtered.length === 0 ? (
          <s-section>
            <s-stack direction="block" gap="tight" align="center">
              <s-heading>No suggestions yet</s-heading>
              <s-paragraph tone="subdued">
                Products show up here once the matcher finds competitors above
                the threshold. Click Re-suggest to generate.
              </s-paragraph>
            </s-stack>
          </s-section>
        ) : null}

        {filtered.map((p) => (
          <ProductCard key={p.id} product={p} fetcher={fetcher} shop={shop} />
        ))}
      </s-stack>
    </s-page>
  );
}

// ─── Product Card ────────────────────────────────────────────────────────────
function ProductCard({ product, fetcher, shop }) {
  const ps = product.suggestion;
  const initialTitle = ps?.editedTitle ?? ps?.suggestedTitle ?? product.title;
  const initialDesc =
    ps?.editedDescriptionHtml ??
    ps?.suggestedDescriptionHtml ??
    product.description ??
    "";

  const [title, setTitle] = useState(initialTitle);
  const [desc, setDesc] = useState(initialDesc);

  // Variant prices live here (lifted) so Apply can gather them all in one shot.
  const [variantPrices, setVariantPrices] = useState(() => {
    const m = {};
    for (const v of product.variants) {
      const sug = v.priceSuggestion;
      const seed =
        sug?.chosenPrice != null
          ? Number(sug.chosenPrice)
          : sug?.competitorMedian != null
            ? Number(sug.competitorMedian)
            : Number(v.currentPrice);
      m[v.id] = String(seed);
    }
    return m;
  });
  const setVariantPrice = (variantId, value) =>
    setVariantPrices((prev) => ({ ...prev, [variantId]: value }));

  // Per-variant "pick counter" — bumping it remounts the input so a Min/Median/
  // Max pick reliably overwrites the web-component's internal value.
  const [pickCounters, setPickCounters] = useState({});
  const bumpPick = (variantId) =>
    setPickCounters((prev) => ({
      ...prev,
      [variantId]: (prev[variantId] ?? 0) + 1,
    }));

  const isBusyOnThis =
    fetcher.state === "submitting" &&
    fetcher.formData?.get("productId") === product.id;

  const intentBusy = isBusyOnThis ? fetcher.formData.get("intent") : null;

  const saveContent = () => {
    fetcher.submit(
      {
        intent: "saveProductEdits",
        productId: product.id,
        editedTitle: title,
        editedDescriptionHtml: desc,
      },
      { method: "POST" },
    );
  };

  const applyAll = () => {
    if (
      !confirm(
        "Push these edits to your Shopify store? This will update the live product.",
      )
    )
      return;
    // Collect all variant prices in the form so the server persists then
    // applies them in one shot.
    const fd = {
      intent: "applyProduct",
      productId: product.id,
      editedTitle: title,
      editedDescriptionHtml: desc,
    };
    for (const [vid, price] of Object.entries(variantPrices)) {
      fd[`variantPrice__${vid}`] = price ?? "";
    }
    fetcher.submit(fd, { method: "POST" });
  };

  const storeUrl = productStoreUrl(shop, product.handle);

  return (
    <s-section>
      <s-stack direction="block" gap="base">
        <s-stack direction="inline" gap="base" align="center">
          {product.imageUrl ? (
            <img
              src={product.imageUrl}
              alt={product.title}
              width="56"
              height="56"
              style={{ objectFit: "cover", borderRadius: "8px" }}
            />
          ) : null}
          <s-stack direction="block" gap="none">
            <s-stack direction="inline" gap="tight" align="center">
              <s-text emphasis="bold">{product.title}</s-text>
              {storeUrl ? (
                <s-link href={storeUrl} target="_blank" tone="subdued">
                  View on store ↗
                </s-link>
              ) : null}
            </s-stack>
            <s-stack direction="inline" gap="tight" align="center">
              {ps ? (
                <s-badge tone={statusTone(ps.status)}>{ps.status}</s-badge>
              ) : null}
              <s-text tone="subdued">
                {product.variants.length} variant
                {product.variants.length === 1 ? "" : "s"} ·{" "}
                {ps?.matchCount ?? 0} competitor
                {ps?.matchCount === 1 ? "" : "s"}
                {priceRange(product.variants)
                  ? ` · Current ${priceRange(product.variants)}`
                  : ""}
              </s-text>
            </s-stack>
          </s-stack>
          <s-spacer />
          <s-button
            variant="plain"
            onClick={() =>
              fetcher.submit(
                { intent: "regenerateProduct", productId: product.id },
                { method: "POST" },
              )
            }
            disabled={intentBusy === "regenerateProduct"}
          >
            {intentBusy === "regenerateProduct" ? "Queuing…" : "Re-suggest this"}
          </s-button>
          <s-button
            variant="plain"
            tone="critical"
            onClick={() =>
              fetcher.submit(
                { intent: "skipProduct", productId: product.id },
                { method: "POST" },
              )
            }
            disabled={intentBusy === "skipProduct"}
          >
            Skip
          </s-button>
        </s-stack>

        <s-divider />

        {/* ── Current product (read-only) ─────────────────────────── */}
        <CurrentPanel product={product} />

        <s-divider />

        {/* ── Content edits ───────────────────────────────────────── */}
        {ps?.suggestedTitle || ps?.suggestedDescriptionHtml ? (
          <s-stack direction="block" gap="tight">
            <s-text emphasis="bold">Suggested title & description</s-text>
            <s-text-field
              label="Title"
              value={title}
              onInput={(e) => setTitle(e.currentTarget.value)}
              helpText={
                ps?.suggestedTitle && ps.suggestedTitle !== title
                  ? `Suggested: ${ps.suggestedTitle}`
                  : undefined
              }
            />
            <s-text-area
              label="Description (HTML)"
              rows={6}
              value={desc}
              onInput={(e) => setDesc(e.currentTarget.value)}
            />
            {ps?.contentRationale ? (
              <s-text tone="subdued">Why: {ps.contentRationale}</s-text>
            ) : null}
            <s-button
              variant="secondary"
              onClick={saveContent}
              disabled={intentBusy === "saveProductEdits"}
            >
              {intentBusy === "saveProductEdits" ? "Saving…" : "Save edits"}
            </s-button>
          </s-stack>
        ) : (
          <s-paragraph tone="subdued">
            No content suggestion generated yet.
          </s-paragraph>
        )}

        <s-divider />

        {/* ── Per-variant prices ──────────────────────────────────── */}
        <s-text emphasis="bold">Variant prices</s-text>
        <s-stack direction="block" gap="tight">
          {product.variants.map((v) => (
            <VariantRow
              key={v.id}
              variant={v}
              fetcher={fetcher}
              chosen={variantPrices[v.id] ?? ""}
              setChosen={(value) => setVariantPrice(v.id, value)}
              pickCounter={pickCounters[v.id] ?? 0}
              onPick={(value) => {
                setVariantPrice(v.id, value);
                bumpPick(v.id);
              }}
              isBusyOnThis={
                fetcher.state === "submitting" &&
                fetcher.formData?.get("variantId") === v.id
              }
            />
          ))}
        </s-stack>

        <s-divider />

        <s-stack direction="inline" gap="base" align="center">
          <s-spacer />
          <s-button
            variant="primary"
            onClick={applyAll}
            disabled={intentBusy === "applyProduct"}
          >
            {intentBusy === "applyProduct" ? "Applying…" : "Apply to Shopify"}
          </s-button>
        </s-stack>
      </s-stack>
    </s-section>
  );
}

// ─── Current Product Panel (read-only) ──────────────────────────────────────
function CurrentPanel({ product }) {
  const [expanded, setExpanded] = useState(false);
  const desc = product.description || "";
  const isLong = desc.length > 280;
  const shown = expanded || !isLong ? desc : desc.slice(0, 280) + "…";

  return (
    <s-box padding="base" background="subdued" borderRadius="base">
      <s-stack direction="block" gap="tight">
        <s-text emphasis="bold" tone="subdued">Current on Shopify</s-text>
        <s-stack direction="block" gap="none">
          <s-text tone="subdued">Title</s-text>
          <s-text>{product.title}</s-text>
        </s-stack>
        {desc ? (
          <s-stack direction="block" gap="none">
            <s-text tone="subdued">Description</s-text>
            <div
              style={{ fontSize: "0.875rem", lineHeight: 1.4 }}
              dangerouslySetInnerHTML={{ __html: shown }}
            />
            {isLong ? (
              <s-button variant="plain" onClick={() => setExpanded((x) => !x)}>
                {expanded ? "Show less" : "Show more"}
              </s-button>
            ) : null}
          </s-stack>
        ) : (
          <s-text tone="subdued">No description set.</s-text>
        )}
      </s-stack>
    </s-box>
  );
}

// ─── Variant Row ─────────────────────────────────────────────────────────────
function VariantRow({
  variant,
  fetcher,
  chosen,
  setChosen,
  pickCounter,
  onPick,
  isBusyOnThis,
}) {
  const sug = variant.priceSuggestion;
  const current = Number(variant.currentPrice);
  const intentBusy = isBusyOnThis ? fetcher.formData.get("intent") : null;

  const save = () => {
    fetcher.submit(
      {
        intent: "saveVariantPrice",
        variantId: variant.id,
        chosenPrice: chosen,
      },
      { method: "POST" },
    );
  };

  return (
    <s-box padding="base" borderWidth="base" borderRadius="base">
      <s-stack direction="block" gap="tight">
        <s-stack direction="inline" gap="base" align="center">
          <s-text emphasis="bold">{variant.title}</s-text>
          {sug ? (
            <s-badge tone={statusTone(sug.status)}>{sug.status}</s-badge>
          ) : null}
          <s-spacer />
          <s-text tone="subdued">Current</s-text>
          <s-text emphasis="bold">{formatINR(current)}</s-text>
        </s-stack>

        {sug && sug.competitorCount > 0 ? (
          <s-stack direction="inline" gap="base" align="center">
            <s-button
              variant="plain"
              onClick={() => onPick(String(sug.competitorMin))}
            >
              Min {formatINR(sug.competitorMin)}
            </s-button>
            <s-button
              variant="plain"
              onClick={() => onPick(String(sug.competitorMedian))}
            >
              Median {formatINR(sug.competitorMedian)}
            </s-button>
            <s-button
              variant="plain"
              onClick={() => onPick(String(sug.competitorMax))}
            >
              Max {formatINR(sug.competitorMax)}
            </s-button>
            <s-text tone="subdued">
              from {sug.competitorCount} competitor
              {sug.competitorCount === 1 ? "" : "s"}
            </s-text>
          </s-stack>
        ) : (
          <s-paragraph tone="subdued">
            No usable competitor prices for this variant.
          </s-paragraph>
        )}

        <s-stack direction="inline" gap="base" align="center">
          {/* key forces a fresh mount whenever a pick happens, so the web
              component picks up the new default value. Typing doesn't bump the
              key, so focus is preserved while typing. */}
          <s-text-field
            key={`${variant.id}-${pickCounter}`}
            label="Your new price (₹)"
            type="number"
            defaultValue={chosen}
            onInput={(e) => setChosen(e.currentTarget.value)}
          />
          <s-button
            variant="secondary"
            onClick={save}
            disabled={intentBusy === "saveVariantPrice"}
          >
            {intentBusy === "saveVariantPrice" ? "Saving…" : "Save"}
          </s-button>
          <s-button
            variant="plain"
            tone="critical"
            onClick={() =>
              fetcher.submit(
                { intent: "skipVariant", variantId: variant.id },
                { method: "POST" },
              )
            }
            disabled={intentBusy === "skipVariant"}
          >
            Skip
          </s-button>
        </s-stack>

        {sug?.priceRationale ? (
          <s-text tone="subdued">Why: {sug.priceRationale}</s-text>
        ) : null}
      </s-stack>
    </s-box>
  );
}

export function ErrorBoundary() {
  const error = useRouteError();
  console.error("[Suggestions ErrorBoundary]", error);
  return boundary.error(error);
}

export const headers = (headersArgs) => {
  return boundary.headers(headersArgs);
};
