import { useEffect, useState } from "react";
import { useFetcher, useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import db from "../db.server";
import { authenticate } from "../shopify.server";
import { refineQuery } from "../lib/queryRefiner.server";

export const loader = async ({ request, params }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain  = session.shop;
  const productId   = params.id;

  const product = await db.shopifyProduct.findFirst({
    where: { id: productId, shopDomain },
  });
  if (!product) throw new Response("Product not found", { status: 404 });

  const settings = await db.shopSettings.findUnique({
    where: { shopDomain },
  });

  const latestJob = await db.discoveryJob.findFirst({
    where: { shopifyProductId: productId },
    orderBy: { requestedAt: "desc" },
  });

  const candidates = await db.competitorCandidate.findMany({
    where: { shopifyProductId: productId },
    orderBy: { discoveredAt: "desc" },
    take: 100,
  });

  // Listing-cap fallback chain: product override → shop default → hard 5.
  const listingExpansionCapDefault =
    product.listingExpansionCap ?? settings?.listingExpansionCap ?? 5;

  return {
    product: {
      id: product.id,
      title: product.title,
      vendor: product.vendor,
      categoryTop: product.categoryTop,
      description: product.description,
      imageUrl: product.imageUrl,
      searchQuery: product.searchQuery,
      searchQueryOverride: product.searchQueryOverride,
      dynamicPricingEnabled: product.dynamicPricingEnabled,
      discoveryNumResults: product.discoveryNumResults ?? 10,
      listingExpansionCap: listingExpansionCapDefault,
    },
    latestJob,
    candidates: candidates.map((c) => ({
      id: c.id,
      url: c.url,
      domain: c.domain,
      serpTitle: c.serpTitle,
      serpSnippet: c.serpSnippet,
      status: c.status,
      discoveredAt: c.discoveredAt,
    })),
  };
};

export const action = async ({ request, params }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain  = session.shop;
  const productId   = params.id;
  const form        = await request.formData();
  const intent      = form.get("intent");

  const product = await db.shopifyProduct.findFirst({
    where: { id: productId, shopDomain },
  });
  if (!product) return { error: "product_not_found" };

  if (intent === "refine") {
    const hint = (form.get("hint") || "").toString();
    try {
      const refined = await refineQuery({
        title:       product.title,
        vendor:      product.vendor,
        category:    product.categoryTop,
        description: product.description,
        hint,
      });
      return { refined };
    } catch (e) {
      return { error: String(e.message || e) };
    }
  }

  if (intent === "toggleDynamic") {
    // Clearing lastDiscoveryAt on toggle-off re-arms first-time discovery,
    // so a subsequent off→on cycle re-runs Serper from scratch.
    const enabled = form.get("enabled") === "true";
    await db.shopifyProduct.update({
      where: { id: productId },
      data: {
        dynamicPricingEnabled: enabled,
        ...(enabled ? {} : { lastDiscoveryAt: null }),
      },
    });
    return { toggled: enabled };
  }

  if (intent === "search") {
    const query      = (form.get("query") || "").toString().trim();
    const numResults = Math.max(1, Math.min(parseInt(form.get("numResults") || "10", 10) || 10, 50));
    // listingCap is optional; null lets the resolver fall back to ShopifyProduct/ShopSettings.
    const rawCap = form.get("listingExpansionCap");
    const listingExpansionCap =
      rawCap == null || rawCap === ""
        ? null
        : Math.max(1, Math.min(parseInt(rawCap, 10) || 5, 50));
    if (!query) return { error: "empty_query" };

    const job = await db.discoveryJob.create({
      data: {
        shopDomain,
        shopifyProductId: productId,
        status: "QUEUED",
        query,
        numResults,
        listingExpansionCap,
      },
    });
    return { queued: true, jobId: job.id };
  }

  return { error: "unknown_intent" };
};

export default function DiscoverPage() {
  const { product, latestJob, candidates } = useLoaderData();
  const refineFetcher = useFetcher();
  const searchFetcher = useFetcher();
  const toggleFetcher = useFetcher();

  // Optimistic toggle: prefer the in-flight value while submitting, else the latest server state.
  const submittingToggle = toggleFetcher.formData?.get("enabled");
  const dynamicOn = submittingToggle != null
    ? submittingToggle === "true"
    : product.dynamicPricingEnabled;

  const initialQuery = product.searchQueryOverride || product.searchQuery || "";
  const [query, setQuery] = useState(initialQuery);
  const [hint, setHint]   = useState("");
  const [num, setNum]     = useState(product.discoveryNumResults ?? 10);
  const [listingCap, setListingCap] = useState(product.listingExpansionCap ?? 5);

  useEffect(() => {
    if (refineFetcher.state === "idle" && refineFetcher.data?.refined) {
      setQuery(refineFetcher.data.refined);
    }
  }, [refineFetcher.state, refineFetcher.data]);

  const refining = refineFetcher.state !== "idle";
  const searching = searchFetcher.state !== "idle";

  const doRefine = () => {
    refineFetcher.submit({ intent: "refine", hint }, { method: "POST" });
  };
  const doSearch = () => {
    if (!query.trim()) return;
    const n = Math.max(1, Math.min(parseInt(num, 10) || 10, 50));
    const cap = Math.max(1, Math.min(parseInt(listingCap, 10) || 5, 50));
    searchFetcher.submit(
      { intent: "search", query, numResults: String(n), listingExpansionCap: String(cap) },
      { method: "POST" },
    );
  };

  return (
    <s-page heading={`Find competitors: ${product.title}`}>
      <s-section>
        <s-stack direction="inline" gap="base" align="center">
          {product.imageUrl && (
            <img src={product.imageUrl} alt={product.title} width="64" height="64" style={{ objectFit: "cover", borderRadius: 4 }} />
          )}
          <s-stack direction="block" gap="tight">
            <s-text emphasis="bold">{product.title}</s-text>
            <s-text tone="subdued">
              {product.vendor ? `${product.vendor} · ` : ""}{product.categoryTop || ""}
            </s-text>
          </s-stack>
          <s-stack direction="inline" gap="tight" align="center">
            <s-badge tone={dynamicOn ? "success" : "subdued"}>
              Dynamic pricing: {dynamicOn ? "ON" : "OFF"}
            </s-badge>
            <s-button
              size="slim"
              variant={dynamicOn ? "plain" : "primary"}
              onClick={() =>
                toggleFetcher.submit(
                  { intent: "toggleDynamic", enabled: dynamicOn ? "false" : "true" },
                  { method: "POST" },
                )
              }
              disabled={toggleFetcher.state !== "idle" || undefined}
            >
              {dynamicOn ? "Turn off" : "Turn on"}
            </s-button>
          </s-stack>
        </s-stack>
      </s-section>

      <s-section heading="Search query">
        <s-stack direction="block" gap="base">
          <s-text tone="subdued">
            The more specific the query, the better the seller links. Use "Refine with AI" to
            turn a rough phrase into a high-precision search.
          </s-text>

          <s-text-field
            label="Optional hint for AI refinement"
            placeholder="e.g. green wayfarer style, unisex"
            value={hint}
            onChange={(e) => setHint(e.target.value)}
          />
          <s-button onClick={doRefine} disabled={refining || undefined}>
            {refining ? "Refining…" : "Refine with AI"}
          </s-button>
          {refineFetcher.data?.error && (
            <s-text tone="critical">Refine failed: {refineFetcher.data.error}</s-text>
          )}

          <s-text-field
            label="Search query (editable)"
            placeholder="e.g. green wayfarer sunglasses for men and women"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />

          <s-stack direction="inline" gap="base" align="end">
            <s-text-field
              label="Number of products"
              type="number"
              value={String(num)}
              onInput={(e) => setNum(e.currentTarget.value)}
              min="1"
              max="50"
            />
            <s-text-field
              label="Max products if listing page"
              type="number"
              value={String(listingCap)}
              onInput={(e) => setListingCap(e.currentTarget.value)}
              min="1"
              max="50"
            />
            <s-button variant="primary" onClick={doSearch} disabled={searching || !query.trim() || undefined}>
              {searching ? "Queuing…" : "Search & save"}
            </s-button>
          </s-stack>
          {searchFetcher.data?.queued && (
            <s-text tone="success">
              Queued. The scheduler picks it up within ~30s. Refresh this page to see results.
            </s-text>
          )}
          {searchFetcher.data?.error && (
            <s-text tone="critical">Search failed: {searchFetcher.data.error}</s-text>
          )}
        </s-stack>
      </s-section>

      {latestJob && (
        <s-section heading="Latest job">
          <s-stack direction="inline" gap="base" align="center">
            <s-badge tone={
              latestJob.status === "COMPLETED" ? "success"
              : latestJob.status === "FAILED" ? "critical"
              : "info"
            }>
              {latestJob.status}
            </s-badge>
            <s-text>{latestJob.candidateCount} candidates</s-text>
            {latestJob.query && <s-text tone="subdued">q: {latestJob.query}</s-text>}
            {latestJob.error && <s-text tone="critical">err: {latestJob.error}</s-text>}
          </s-stack>
        </s-section>
      )}

      <s-section heading={`Discovered links (${candidates.length})`}>
        {candidates.length === 0 ? (
          <s-text tone="subdued">No candidates yet. Run a search above.</s-text>
        ) : (
          <s-resource-list>
            {candidates.map((c) => (
              <s-resource-item key={c.id} id={c.id}>
                <s-stack direction="block" gap="tight">
                  <s-stack direction="inline" gap="base" align="center">
                    <s-text emphasis="bold">{c.serpTitle || "(no title)"}</s-text>
                    <s-badge>{c.domain}</s-badge>
                    <s-badge tone={c.status === "VERIFIED" || c.status === "SCRAPED" ? "success" : c.status === "REJECTED" || c.status === "DEAD" ? "critical" : "info"}>
                      {c.status}
                    </s-badge>
                  </s-stack>
                  {c.serpSnippet && <s-text tone="subdued">{c.serpSnippet}</s-text>}
                  <s-link href={c.url} target="_blank">{c.url}</s-link>
                </s-stack>
              </s-resource-item>
            ))}
          </s-resource-list>
        )}
      </s-section>
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
