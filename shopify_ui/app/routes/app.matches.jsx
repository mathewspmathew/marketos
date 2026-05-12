import { useMemo, useState } from "react";
import { useFetcher, useLoaderData, useRouteError } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

// ─── Loader ──────────────────────────────────────────────────────────────────
export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const url = new URL(request.url);
  const showDismissed = url.searchParams.get("showDismissed") === "1";

  const matches = await db.productMatch.findMany({
    where: {
      shopDomain: shop,
      ...(showDismissed ? {} : { dismissedAt: null }),
    },
    orderBy: [{ matchScore: "desc" }],
    include: {
      shopifyVariant: {
        include: { product: true },
      },
      competitorVariant: true,
      competitorProduct: {
        include: { urls: { take: 1 } },
      },
    },
  });

  // Group: ShopifyProduct → list of competitor matches
  const byProduct = new Map();
  for (const m of matches) {
    const sv = m.shopifyVariant;
    if (!sv || !sv.product) continue;
    const sp = sv.product;
    if (!byProduct.has(sp.id)) {
      byProduct.set(sp.id, {
        id:          sp.id,
        title:       sp.title,
        imageUrl:    sp.imageUrl,
        productType: sp.productType,
        handle:      sp.handle,
        storeUrl:    sp.handle ? `https://${shop}/products/${sp.handle}` : null,
        matches:     [],
      });
    }
    byProduct.get(sp.id).matches.push({
      id:                m.id,
      shopifyVariantId:  sv.id,
      shopifyVariantTitle: sv.title,
      shopifyPrice:      Number(sv.currentPrice),
      competitorTitle:   m.competitorProduct?.title || "(unknown)",
      competitorDomain:  m.competitorProduct?.domain || "",
      competitorUrl:     m.competitorProduct?.urls?.[0]?.url || null,
      competitorPrice:   m.competitorVariant ? Number(m.competitorVariant.currentPrice) : null,
      competitorVariantTitle: m.competitorVariant?.title || null,
      matchScore:        Number(m.matchScore),
      matchedAt:         m.matchedAt,
      dismissedAt:       m.dismissedAt,
    });
  }

  const products = Array.from(byProduct.values()).sort(
    (a, b) => b.matches.length - a.matches.length,
  );

  return { products, totalMatches: matches.length, shop, showDismissed };
};

// ─── Action ──────────────────────────────────────────────────────────────────
// Soft-delete a ProductMatch (or restore it) and recompute the affected
// VariantPriceSuggestion + ProductSuggestion stats inline so the Suggestions
// page reflects the change immediately. We keep the row so the matcher worker
// won't resurrect it on the next upsert.
export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const formData = await request.formData();
  const intent = formData.get("intent");
  const matchId = formData.get("matchId");

  if (intent !== "dismissMatch" && intent !== "restoreMatch") {
    return { ok: false, error: `Unknown intent: ${intent}` };
  }

  const match = await db.productMatch.findFirst({
    where: { id: matchId, shopDomain: shop },
    select: { id: true, shopifyVariantId: true },
  });
  if (!match) return { ok: false, error: "Forbidden" };

  await db.productMatch.update({
    where: { id: matchId },
    data: { dismissedAt: intent === "dismissMatch" ? new Date() : null },
  });

  await recomputeForVariant(shop, match.shopifyVariantId);
  return { ok: true };
};

// Recompute price-suggestion stats from the currently-live matches for one
// variant, then roll up product-level matchCount/avgMatchScore. Mirrors what
// the suggestion worker would compute, minus the LLM call — we leave the
// suggested title/description untouched.
async function recomputeForVariant(shop, shopifyVariantId) {
  const variant = await db.shopifyVariant.findUnique({
    where: { id: shopifyVariantId },
    select: { productId: true },
  });
  if (!variant) return;

  const liveMatches = await db.productMatch.findMany({
    where: { shopifyVariantId, dismissedAt: null },
    include: { competitorVariant: { select: { currentPrice: true } } },
  });

  const prices = liveMatches
    .map((m) => m.competitorVariant?.currentPrice)
    .filter((p) => p != null)
    .map((p) => Number(p))
    .filter((n) => Number.isFinite(n) && n > 0)
    .sort((a, b) => a - b);

  const count = prices.length;
  const min = count ? prices[0] : null;
  const max = count ? prices[count - 1] : null;
  const median = count
    ? count % 2
      ? prices[(count - 1) / 2]
      : (prices[count / 2 - 1] + prices[count / 2]) / 2
    : null;

  const existing = await db.variantPriceSuggestion.findUnique({
    where: { shopifyVariantId },
    select: { status: true },
  });
  if (existing) {
    await db.variantPriceSuggestion.update({
      where: { shopifyVariantId },
      data: {
        competitorMin: min,
        competitorMedian: median,
        competitorMax: max,
        competitorCount: count,
      },
    });
  }

  // Product-level rollup
  const productLiveMatches = await db.productMatch.findMany({
    where: {
      shopifyVariant: { productId: variant.productId },
      dismissedAt: null,
    },
    select: { matchScore: true },
  });
  const productCount = productLiveMatches.length;
  const avg = productCount
    ? productLiveMatches.reduce((s, m) => s + Number(m.matchScore), 0) /
      productCount
    : null;

  await db.productSuggestion.updateMany({
    where: { shopifyProductId: variant.productId, shopDomain: shop },
    data: {
      matchCount: productCount,
      avgMatchScore: avg != null ? avg.toFixed(2) : null,
    },
  });
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function priceDelta(yours, competitor) {
  if (competitor == null || !yours) return null;
  const diff = competitor - yours;
  const pct = (diff / yours) * 100;
  return { diff, pct };
}

function scoreTone(score) {
  if (score >= 85) return "success";
  if (score >= 70) return "info";
  return "subdued";
}

// ─── UI ──────────────────────────────────────────────────────────────────────
export default function MatchesPage() {
  const { products, totalMatches, showDismissed } = useLoaderData();
  const fetcher = useFetcher();
  const [query, setQuery] = useState("");

  const toggleDismissed = () => {
    const next = new URL(window.location.href);
    if (showDismissed) next.searchParams.delete("showDismissed");
    else next.searchParams.set("showDismissed", "1");
    window.location.href = next.toString();
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return products;
    return products.filter((p) => p.title.toLowerCase().includes(q));
  }, [products, query]);

  if (totalMatches === 0) {
    return (
      <s-page heading="Matched Products">
        <s-section>
          <s-stack direction="block" gap="base" align="center">
            <s-heading>No matches yet</s-heading>
            <s-paragraph>
              Add a competitor in Controller and wait for the next scrape and
              match cycle to finish.
            </s-paragraph>
          </s-stack>
        </s-section>
      </s-page>
    );
  }

  return (
    <s-page
      heading="Matched Products"
      subheading={`${totalMatches} match${totalMatches === 1 ? "" : "es"} across ${products.length} product${products.length === 1 ? "" : "s"}`}
    >
      <s-stack direction="block" gap="loose">
        <s-section>
          <s-stack direction="inline" gap="base" align="center">
            <s-text-field
              label="Search your products"
              placeholder="Search by product name…"
              value={query}
              onInput={(e) => setQuery(e.currentTarget.value)}
              clearButton
              onClearButtonClick={() => setQuery("")}
            />
            <s-spacer />
            <s-button variant="secondary" onClick={toggleDismissed}>
              {showDismissed ? "Hide removed" : "Show removed"}
            </s-button>
          </s-stack>
        </s-section>

        {filtered.length === 0 ? (
          <s-section>
            <s-paragraph tone="subdued">
              No products match “{query}”.
            </s-paragraph>
          </s-section>
        ) : null}

        {filtered.map((p) => (
          <s-section key={p.id}>
            <s-stack direction="block" gap="base">
              {/* Product header */}
              <s-stack direction="inline" gap="base" align="center">
                {p.imageUrl ? (
                  <img
                    src={p.imageUrl}
                    alt={p.title}
                    width="56"
                    height="56"
                    style={{ objectFit: "cover", borderRadius: "8px" }}
                  />
                ) : null}
                <s-stack direction="block" gap="none">
                  <s-text emphasis="bold">{p.title}</s-text>
                  <s-stack direction="inline" gap="tight" align="center">
                    {p.productType ? (
                      <s-badge>{p.productType}</s-badge>
                    ) : null}
                    <s-text tone="subdued">
                      {p.matches.length} competitor
                      {p.matches.length === 1 ? "" : "s"}
                    </s-text>
                  </s-stack>
                </s-stack>
                <s-spacer />
                {p.storeUrl ? (
                  <s-link href={p.storeUrl} target="_blank">
                    View in store
                  </s-link>
                ) : null}
              </s-stack>

              <s-divider />

              {/* Competitor matches */}
              <s-stack direction="block" gap="tight">
                {p.matches.map((m) => {
                  const delta = priceDelta(m.shopifyPrice, m.competitorPrice);
                  const isDismissed = !!m.dismissedAt;
                  const isBusyOnThis =
                    fetcher.state !== "idle" &&
                    fetcher.formData?.get("matchId") === m.id;
                  const handleDismiss = () => {
                    if (!isDismissed && !confirm("Remove this match? It will be excluded from suggestions.")) return;
                    fetcher.submit(
                      {
                        intent: isDismissed ? "restoreMatch" : "dismissMatch",
                        matchId: m.id,
                      },
                      { method: "POST" },
                    );
                  };
                  return (
                    <s-box
                      key={m.id}
                      padding="base"
                      borderWidth="base"
                      borderRadius="base"
                    >
                      <s-stack direction="inline" gap="base" align="center">
                        <s-badge tone={scoreTone(m.matchScore)}>
                          {m.matchScore.toFixed(0)}%
                        </s-badge>

                        <s-stack direction="block" gap="none">
                          <s-text emphasis="bold">{m.competitorTitle}</s-text>
                          <s-text tone="subdued">
                            {m.competitorDomain}
                            {m.competitorVariantTitle &&
                            m.competitorVariantTitle !== "Default Title"
                              ? ` · ${m.competitorVariantTitle}`
                              : ""}
                          </s-text>
                        </s-stack>

                        <s-spacer />

                        <s-stack direction="block" gap="none" align="end">
                          <s-text tone="subdued">Yours</s-text>
                          <s-text emphasis="bold">
                            ${m.shopifyPrice.toFixed(2)}
                          </s-text>
                        </s-stack>

                        <s-stack direction="block" gap="none" align="end">
                          <s-text tone="subdued">Competitor</s-text>
                          <s-text emphasis="bold">
                            {m.competitorPrice != null
                              ? `$${m.competitorPrice.toFixed(2)}`
                              : "—"}
                          </s-text>
                        </s-stack>

                        {delta ? (
                          <s-badge
                            tone={
                              delta.diff > 0
                                ? "success"
                                : delta.diff < 0
                                ? "critical"
                                : "subdued"
                            }
                          >
                            {delta.diff > 0 ? "+" : ""}
                            {delta.pct.toFixed(1)}%
                          </s-badge>
                        ) : null}

                        {m.competitorUrl ? (
                          <s-link href={m.competitorUrl} target="_blank">
                            View
                          </s-link>
                        ) : null}

                        {isDismissed ? (
                          <s-badge tone="subdued">Removed</s-badge>
                        ) : null}
                        <s-button
                          variant="plain"
                          tone={isDismissed ? undefined : "critical"}
                          onClick={handleDismiss}
                          disabled={isBusyOnThis}
                        >
                          {isBusyOnThis
                            ? "Saving…"
                            : isDismissed
                              ? "Restore"
                              : "Remove"}
                        </s-button>
                      </s-stack>
                    </s-box>
                  );
                })}
              </s-stack>
            </s-stack>
          </s-section>
        ))}
      </s-stack>
    </s-page>
  );
}

export function ErrorBoundary() {
  const error = useRouteError();
  console.error("[Matches ErrorBoundary]", error);
  return boundary.error(error);
}

export const headers = (headersArgs) => {
  return boundary.headers(headersArgs);
};
