import { useLoaderData, useRouteError } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

// ─── Loader ──────────────────────────────────────────────────────────────────
export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;

  const matches = await db.productMatch.findMany({
    where: { shopDomain: shop },
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

  // Group: ShopifyProduct → list of competitor matches (with shopify variant + price)
  const byProduct = new Map();
  for (const m of matches) {
    const sv = m.shopifyVariant;
    if (!sv || !sv.product) continue;
    const sp = sv.product;
    if (!byProduct.has(sp.id)) {
      byProduct.set(sp.id, {
        id:        sp.id,
        title:     sp.title,
        imageUrl:  sp.imageUrl,
        productType: sp.productType,
        matches:   [],
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
    });
  }

  const products = Array.from(byProduct.values()).sort((a, b) => b.matches.length - a.matches.length);

  return { products, totalMatches: matches.length, shop };
};

// ─── UI ──────────────────────────────────────────────────────────────────────
export default function MatchesPage() {
  const { products, totalMatches } = useLoaderData();

  return (
    <s-page heading="Matched Products">
      <s-stack direction="block" gap="loose">
        <s-section>
          <s-paragraph>
            {totalMatches === 0
              ? "No matches yet. Add a competitor in Controller and wait for the scrape + match cycle to finish."
              : `${totalMatches} match${totalMatches === 1 ? "" : "es"} across ${products.length} of your products.`}
          </s-paragraph>
        </s-section>

        {products.map((p) => (
          <s-section key={p.id} heading={p.title}>
            <s-stack direction="block" gap="base">
              {p.productType ? (
                <s-text tone="subdued">Type: {p.productType}</s-text>
              ) : null}

              <s-stack direction="block" gap="tight">
                {p.matches.map((m) => (
                  <s-stack
                    key={m.id}
                    direction="inline"
                    gap="loose"
                    align="center"
                  >
                    <s-badge tone="info">{m.matchScore.toFixed(1)}%</s-badge>
                    <s-stack direction="block" gap="none">
                      <s-text emphasis="bold">{m.competitorTitle}</s-text>
                      <s-text tone="subdued">
                        {m.competitorDomain}
                        {m.competitorVariantTitle && m.competitorVariantTitle !== "Default Title"
                          ? ` · ${m.competitorVariantTitle}`
                          : ""}
                      </s-text>
                    </s-stack>
                    <s-text>
                      {m.competitorPrice != null
                        ? `Competitor: ${m.competitorPrice.toFixed(2)}`
                        : "Competitor: —"}
                    </s-text>
                    <s-text tone="subdued">
                      Yours: {m.shopifyPrice.toFixed(2)}
                    </s-text>
                    {m.competitorUrl ? (
                      <s-link href={m.competitorUrl} target="_blank">
                        View
                      </s-link>
                    ) : null}
                  </s-stack>
                ))}
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
