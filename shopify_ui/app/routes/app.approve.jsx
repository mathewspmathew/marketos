/* eslint-disable react/prop-types */
import { useFetcher, useLoaderData, useRouteError, Link } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

// Approve queue: borderline LIKELY matches that need a human eye to promote
// to CONFIRMED. CONFIRMED matches don't surface (the matcher is already
// certain); WEAK matches don't surface (clearly noise). Approving here is
// what makes a product show up on /app/dynamic.
export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;

  const pending = await db.productLevelMatch.findMany({
    where: {
      shopDomain: shop,
      confidenceTier: "LIKELY",
      reviewedAt: null,
      rejectedByMerchant: false,
    },
    orderBy: [{ confidence: "desc" }, { createdAt: "desc" }],
    take: 50,
    include: {
      ShopifyProduct: {
        select: {
          id: true, title: true, vendor: true, productType: true,
          imageUrl: true, categoryTop: true, productGender: true,
          ShopifyVariant: {
            select: { id: true, title: true, currentPrice: true, imageUrl: true },
            orderBy: { currentPrice: "asc" },
            take: 1,
          },
        },
      },
      ScrapedProduct: {
        select: {
          id: true, title: true, vendor: true, productType: true, domain: true,
          imageUrl: true, categoryTop: true, productGender: true,
          ScrapedVariant: {
            select: { id: true, title: true, currentPrice: true },
            orderBy: { currentPrice: "asc" },
            take: 1,
          },
        },
      },
    },
  });

  // Sanity hint for the header — how much triage has already been done.
  const reviewedCount = await db.productLevelMatch.count({
    where: { shopDomain: shop, reviewedAt: { not: null } },
  });
  // And how many are confirmed (live on /app/dynamic) — gives the merchant
  // a sense of the funnel: pending → confirmed.
  const confirmedCount = await db.productLevelMatch.count({
    where: {
      shopDomain: shop,
      OR: [{ confidenceTier: "CONFIRMED" }, { confirmedByMerchant: true }],
    },
  });

  return {
    shop,
    reviewedCount,
    confirmedCount,
    pending: pending.map((p) => ({
      id:                 p.id,
      confidence:         Number(p.confidence),
      createdAt:          p.createdAt,
      shopifyProduct: {
        ...p.ShopifyProduct,
        sampleVariant: p.ShopifyProduct.ShopifyVariant[0]
          ? { ...p.ShopifyProduct.ShopifyVariant[0],
              currentPrice: Number(p.ShopifyProduct.ShopifyVariant[0].currentPrice) }
          : null,
      },
      scrapedProduct: {
        ...p.ScrapedProduct,
        sampleVariant: p.ScrapedProduct.ScrapedVariant[0]
          ? { ...p.ScrapedProduct.ScrapedVariant[0],
              currentPrice: Number(p.ScrapedProduct.ScrapedVariant[0].currentPrice) }
          : null,
      },
    })),
  };
};

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const fd = await request.formData();
  const intent = fd.get("intent");
  const id = String(fd.get("id") || "");
  if (!id) return { ok: false, error: "missing_id" };

  // Defense in depth — confirm the row belongs to this shop before mutating.
  const existing = await db.productLevelMatch.findFirst({
    where: { id, shopDomain: shop },
  });
  if (!existing) return { ok: false, error: "not_found" };

  if (intent === "approve") {
    await db.productLevelMatch.update({
      where: { id },
      data: {
        confirmedByMerchant: true,
        rejectedByMerchant:  false,
        reviewedAt:          new Date(),
        // Merchant approval is sufficient evidence regardless of the
        // algorithm's brand check.
        confidenceTier:      "CONFIRMED",
      },
    });
    // Approving = "start dynamic pricing for this product." Flip
    // autoPriceEnabled on every variant so the product immediately appears
    // on /app/dynamic. The next stats recompute (triggered by any new
    // scraper observation) will then fire decide → push automatically.
    await db.shopifyVariant.updateMany({
      where: { productId: existing.shopifyProductId },
      data:  { autoPriceEnabled: true },
    });
    return { ok: true, intent };
  }

  if (intent === "reject") {
    await db.productLevelMatch.update({
      where: { id },
      data: {
        rejectedByMerchant:  true,
        confirmedByMerchant: false,
        reviewedAt:          new Date(),
      },
    });
    // Also drop the underlying ProductMatch rows so they stop feeding stats
    // immediately. The matcher's rejected filter prevents re-inserts.
    await db.productMatch.deleteMany({
      where: { shopDomain: shop, productMatchId: id },
    });
    return { ok: true, intent };
  }

  if (intent === "undo") {
    await db.productLevelMatch.update({
      where: { id },
      data: {
        confirmedByMerchant: false,
        rejectedByMerchant:  false,
        reviewedAt:          null,
      },
    });
    return { ok: true, intent };
  }

  return { ok: false, error: "unknown_intent" };
};

export default function ApprovePage() {
  const { pending, reviewedCount, confirmedCount } = useLoaderData();
  return (
    <s-page heading="Approve matches">
      <s-section>
        <s-text>
          Borderline matches — the algorithm thinks these are likely the same
          product, but not certain enough to act on automatically. Approve to
          start pricing against this competitor, or reject to suppress the
          pair permanently.
        </s-text>
        <s-text tone="subdued" style={{ fontSize: 12, marginTop: 6 }}>
          <strong>{confirmedCount}</strong> confirmed (live on{" "}
          <Link to="/app/dynamic">Dynamic pricing</Link>) ·{" "}
          <strong>{reviewedCount}</strong> reviewed in total
        </s-text>
      </s-section>

      {pending.length === 0 ? (
        <s-section>
          <s-stack gap="tight">
            <s-text emphasis="bold">Nothing to approve right now.</s-text>
            <s-text tone="subdued">
              Everything is either confirmed, rejected, or auto-trusted. New
              candidates appear here after each competitor scrape.
            </s-text>
          </s-stack>
        </s-section>
      ) : (
        <s-stack gap="base">
          {pending.map((m) => <ApproveCard key={m.id} match={m} />)}
        </s-stack>
      )}
    </s-page>
  );
}

function ApproveCard({ match }) {
  const fetcher = useFetcher();
  const busy = fetcher.state !== "idle";
  const my = match.shopifyProduct;
  const cp = match.scrapedProduct;

  return (
    <s-section>
      <s-stack direction="inline" gap="base" alignment="space-between">
        <s-text emphasis="bold">
          Confidence {(match.confidence * 100).toFixed(1)}%
        </s-text>
        <s-text tone="subdued">{cp.domain}</s-text>
      </s-stack>

      <s-stack direction="inline" gap="loose">
        <ProductPanel
          heading="Your product"
          title={my.title}
          vendor={my.vendor}
          productType={my.productType}
          categoryTop={my.categoryTop}
          productGender={my.productGender}
          imageUrl={my.sampleVariant?.imageUrl || my.imageUrl}
          price={my.sampleVariant?.currentPrice}
        />
        <ProductPanel
          heading={`Competitor — ${cp.domain}`}
          title={cp.title}
          vendor={cp.vendor}
          productType={cp.productType}
          categoryTop={cp.categoryTop}
          productGender={cp.productGender}
          imageUrl={cp.sampleVariant?.imageUrl || cp.imageUrl}
          price={cp.sampleVariant?.currentPrice}
        />
      </s-stack>

      <s-stack direction="inline" gap="base" alignment="end">
        <fetcher.Form method="post">
          <input type="hidden" name="intent" value="reject" />
          <input type="hidden" name="id" value={match.id} />
          <s-button type="submit" tone="critical" variant="secondary" disabled={busy}>
            Reject — not the same product
          </s-button>
        </fetcher.Form>

        <fetcher.Form method="post">
          <input type="hidden" name="intent" value="approve" />
          <input type="hidden" name="id" value={match.id} />
          <s-button type="submit" variant="primary" disabled={busy}>
            Approve — same product
          </s-button>
        </fetcher.Form>
      </s-stack>
    </s-section>
  );
}

function ProductPanel({ heading, title, vendor, productType, categoryTop, productGender, imageUrl, price }) {
  return (
    <s-stack gap="tight" style={{ flex: 1 }}>
      <s-text emphasis="bold">{heading}</s-text>
      {imageUrl ? (
        <img src={imageUrl} alt={title}
             style={{ width: "100%", maxHeight: 240, objectFit: "contain",
                      borderRadius: 8, background: "#f6f6f7" }} />
      ) : (
        <s-text tone="subdued">no image</s-text>
      )}
      <s-text>{title}</s-text>
      <s-text tone="subdued">
        brand: {vendor || "—"}{" · "}
        type: {productType || "—"}
      </s-text>
      <s-text tone="subdued">
        category: {categoryTop || "—"}{" · "}
        gender: {productGender || "—"}
      </s-text>
      {price != null ? (
        <s-text emphasis="bold">₹{Number(price).toFixed(2)}</s-text>
      ) : null}
    </s-stack>
  );
}

export function ErrorBoundary() {
  return boundary.error(useRouteError());
}
