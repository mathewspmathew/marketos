import { useFetcher, useLoaderData } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import db from "../db.server";
import { authenticate } from "../shopify.server";

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shopDomain  = session.shop;

  const matches = await db.productLevelMatch.findMany({
    // WEAK matches stay in the DB (matcher uses them as scaffolding) but
    // never surface here — they're too noisy to ask a merchant about.
    where: {
      shopDomain,
      rejectedByMerchant: false,
      confidenceTier: { in: ["CONFIRMED", "LIKELY"] },
    },
    include: {
      shopifyProduct: {
        include: { variants: { take: 1 } },
      },
      scrapedProduct: {
        include: {
          variants: {
            orderBy: { updatedAt: "desc" },
            take: 1,
          },
        },
      },
    },
    orderBy: [{ shopifyProductId: "asc" }, { confidence: "desc" }],
  });

  // Group by Shopify product.
  const byProduct = new Map();
  for (const m of matches) {
    const sp = m.shopifyProduct;
    if (!sp) continue;
    if (!byProduct.has(sp.id)) {
      byProduct.set(sp.id, {
        id: sp.id,
        title: sp.title,
        imageUrl: sp.imageUrl,
        merchantPrice: sp.variants[0]?.currentPrice?.toString() ?? null,
        matches: [],
      });
    }
    const scraped = m.scrapedProduct;
    const variant = scraped?.variants[0];
    byProduct.get(sp.id).matches.push({
      id: m.id,
      confidence:     Number(m.confidence),
      confidenceTier: m.confidenceTier,
      source:         m.source,
      confirmedByMerchant: m.confirmedByMerchant,
      scrapedTitle:   scraped?.title ?? "(missing)",
      scrapedDomain:  scraped?.domain ?? "",
      scrapedImageUrl: scraped?.imageUrl ?? null,
      competitorPrice: variant?.currentPrice?.toString() ?? null,
      competitorUrl:   null,  // ProductUrl lookup below
      scrapedProductId: scraped?.id,
    });
  }

  // Pull the canonical ProductUrl per scraped product for "Open competitor" link.
  const scrapedIds = matches.map((m) => m.scrapedProductId);
  if (scrapedIds.length) {
    const urls = await db.productUrl.findMany({
      where: { prodId: { in: scrapedIds } },
      select: { prodId: true, url: true },
    });
    const urlByScraped = new Map(urls.map((u) => [u.prodId, u.url]));
    for (const grp of byProduct.values()) {
      for (const m of grp.matches) {
        m.competitorUrl = urlByScraped.get(m.scrapedProductId) ?? null;
      }
    }
  }

  return { groups: [...byProduct.values()] };
};

export const action = async ({ request }) => {
  await authenticate.admin(request);
  const formData = await request.formData();
  const matchId = formData.get("matchId");
  const intent  = formData.get("intent");

  if (intent === "confirm") {
    await db.productLevelMatch.update({
      where: { id: matchId },
      data: { confirmedByMerchant: true, rejectedByMerchant: false, reviewedAt: new Date() },
    });
  } else if (intent === "reject") {
    await db.productLevelMatch.update({
      where: { id: matchId },
      data: { confirmedByMerchant: false, rejectedByMerchant: true, reviewedAt: new Date() },
    });
  }
  return null;
};

export default function MatchesPage() {
  const { groups } = useLoaderData();
  const fetcher = useFetcher();

  const act = (matchId, intent) =>
    fetcher.submit({ intent, matchId }, { method: "POST" });

  return (
    <s-page heading="Matched competitors" subheading={`${groups.length} product${groups.length === 1 ? "" : "s"} with matches`}>
      {groups.length === 0 ? (
        <s-section>
          <s-stack direction="block" gap="tight" align="center">
            <s-text emphasis="bold">No matches yet</s-text>
            <s-text tone="subdued">
              Enable Dynamic Pricing on a product to start discovering competitors.
            </s-text>
          </s-stack>
        </s-section>
      ) : (
        groups.map((g) => (
          <s-section key={g.id} heading={g.title}>
            <s-stack direction="inline" gap="base" align="center">
              {g.imageUrl && (
                <img src={g.imageUrl} alt={g.title} width="48" height="48" style={{ objectFit: "cover", borderRadius: 4 }} />
              )}
              {g.merchantPrice && <s-text>Your price: ${g.merchantPrice}</s-text>}
              <s-link href={`/app/history/${encodeURIComponent(g.id)}`}>Price history</s-link>
              <s-link href={`/app/discover/${encodeURIComponent(g.id)}`}>Find competitors</s-link>
            </s-stack>

            <s-resource-list>
              {g.matches.map((m) => (
                <s-resource-item key={m.id} id={m.id}>
                  {m.scrapedImageUrl && (
                    <img slot="media" src={m.scrapedImageUrl} alt={m.scrapedTitle} width="50" height="50" style={{ objectFit: "cover", borderRadius: 4 }} />
                  )}
                  <s-stack direction="block" gap="tight">
                    <s-stack direction="inline" gap="base" align="center">
                      <s-text emphasis="bold">{m.scrapedTitle}</s-text>
                      <s-badge>{m.scrapedDomain}</s-badge>
                      <s-badge tone={m.confidenceTier === "CONFIRMED" ? "success" : "info"}>
                        {m.confidenceTier} ({(m.confidence * 100).toFixed(0)}%)
                      </s-badge>
                      {m.confirmedByMerchant && <s-badge tone="success">Confirmed</s-badge>}
                    </s-stack>
                    <s-stack direction="inline" gap="loose" align="center">
                      {m.competitorPrice && <s-text>Their price: ${m.competitorPrice}</s-text>}
                      {m.competitorUrl && (
                        <s-link href={m.competitorUrl} target="_blank">Open</s-link>
                      )}
                      <s-button size="slim" onClick={() => act(m.id, "confirm")} disabled={m.confirmedByMerchant || undefined}>
                        Confirm
                      </s-button>
                      <s-button size="slim" variant="plain" onClick={() => act(m.id, "reject")}>
                        Reject
                      </s-button>
                    </s-stack>
                  </s-stack>
                </s-resource-item>
              ))}
            </s-resource-list>
          </s-section>
        ))
      )}
    </s-page>
  );
}

export const headers = (h) => boundary.headers(h);
