/* eslint-disable react/prop-types */
import { useState, useEffect, useMemo } from "react";
import { useFetcher, useLoaderData, useRouteError } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

const TIERS = ["PREMIUM", "MIDMARKET", "BUDGET"];

// Loader — list competitors for this shop, backfilling any domains that exist
// in ScrapedProduct but lack a Competitor row yet (e.g. data from before the
// extractor started auto-creating them).
export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;

  const scrapedDomains = await db.scrapedProduct.findMany({
    where: { shopDomain: shop },
    distinct: ["domain"],
    select: { domain: true },
  });
  const existing = await db.competitor.findMany({ where: { shopDomain: shop } });
  const existingDomains = new Set(existing.map((c) => c.domain));

  const toCreate = scrapedDomains
    .map((r) => r.domain)
    .filter((d) => d && d !== "unknown" && !existingDomains.has(d));

  if (toCreate.length) {
    await db.competitor.createMany({
      data: toCreate.map((domain) => ({
        shopDomain: shop,
        domain,
        tier: "MIDMARKET",
        weight: 0.5,
        enabled: true,
      })),
      skipDuplicates: true,
    });
  }

  const competitors = await db.competitor.findMany({
    where: { shopDomain: shop },
    orderBy: [{ enabled: "desc" }, { domain: "asc" }],
  });

  // Count observations per competitor for the "data freshness" hint.
  const obsCounts = await db.competitorPriceObservation.groupBy({
    by: ["competitorId"],
    where: { shopDomain: shop },
    _count: { _all: true },
    _max: { observedAt: true },
  });
  const countMap = Object.fromEntries(
    obsCounts.map((r) => [r.competitorId, { count: r._count._all, last: r._max.observedAt }]),
  );

  return {
    shop,
    competitors: competitors.map((c) => ({
      ...c,
      weight: Number(c.weight),
      observationCount: countMap[c.id]?.count ?? 0,
      lastObservedAt: countMap[c.id]?.last ?? null,
    })),
  };
};

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const formData = await request.formData();
  const intent = formData.get("intent");

  if (intent === "updateCompetitor") {
    const id = formData.get("id");
    const weightRaw = formData.get("weight");
    const tier = formData.get("tier");
    const enabled = formData.get("enabled") === "true";
    const displayName = formData.get("displayName") || null;

    let weight = parseFloat(weightRaw);
    if (Number.isNaN(weight)) weight = 0.5;
    weight = Math.max(0, Math.min(1, weight));

    if (!TIERS.includes(tier)) {
      return { ok: false, error: "Invalid tier" };
    }

    const existing = await db.competitor.findFirst({ where: { id, shopDomain: shop } });
    if (!existing) return { ok: false, error: "Competitor not found for this shop" };

    await db.competitor.update({
      where: { id },
      data: { weight, tier, enabled, displayName },
    });
    return { ok: true };
  }

  return { ok: false, error: "Unknown intent" };
};

function CompetitorRow({ competitor }) {
  const fetcher = useFetcher();
  const [weight, setWeight] = useState(competitor.weight);
  const [tier, setTier] = useState(competitor.tier);
  const [enabled, setEnabled] = useState(competitor.enabled);
  const [displayName, setDisplayName] = useState(competitor.displayName || "");

  // Sync local state if loader data refreshes.
  useEffect(() => {
    setWeight(competitor.weight);
    setTier(competitor.tier);
    setEnabled(competitor.enabled);
    setDisplayName(competitor.displayName || "");
  }, [competitor.id, competitor.weight, competitor.tier, competitor.enabled, competitor.displayName]);

  const dirty =
    weight !== competitor.weight ||
    tier !== competitor.tier ||
    enabled !== competitor.enabled ||
    (displayName || "") !== (competitor.displayName || "");

  const saving =
    fetcher.state === "submitting" &&
    fetcher.formData?.get("intent") === "updateCompetitor" &&
    fetcher.formData?.get("id") === competitor.id;

  const onSave = () => {
    fetcher.submit(
      {
        intent: "updateCompetitor",
        id: competitor.id,
        weight: String(weight),
        tier,
        enabled: String(enabled),
        displayName,
      },
      { method: "POST" },
    );
  };

  const lastSeen = competitor.lastObservedAt
    ? new Date(competitor.lastObservedAt).toLocaleString()
    : "never";

  return (
    <s-section>
      <s-stack direction="block" gap="base">
        <s-stack direction="inline" gap="base" align="center">
          <s-stack direction="block" gap="none">
            <s-text emphasis="bold">{competitor.domain}</s-text>
            <s-text tone="subdued" size="small">
              {competitor.observationCount} price observation
              {competitor.observationCount === 1 ? "" : "s"} · last seen {lastSeen}
            </s-text>
          </s-stack>
          <s-stack direction="inline" gap="base" align="center">
            <s-text>Enabled</s-text>
            <s-toggle
              checked={enabled || undefined}
              onClick={() => setEnabled((v) => !v)}
            />
          </s-stack>
        </s-stack>

        <s-text-field
          label="Display name (optional)"
          placeholder={competitor.domain}
          value={displayName}
          onInput={(e) => setDisplayName(e.currentTarget.value)}
        />

        <s-stack direction="block" gap="tight">
          <s-text emphasis="bold">Tier</s-text>
          <s-stack direction="inline" gap="base">
            {TIERS.map((t) => (
              <s-button
                key={t}
                variant={tier === t ? "primary" : "secondary"}
                onClick={() => setTier(t)}
              >
                {t.charAt(0) + t.slice(1).toLowerCase()}
              </s-button>
            ))}
          </s-stack>
          <s-text tone="subdued" size="small">
            Tier groups competitors for rules like “match cheapest premium competitor”.
          </s-text>
        </s-stack>

        <s-stack direction="block" gap="tight">
          <s-text emphasis="bold">Weight: {weight.toFixed(2)}</s-text>
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={weight}
            onChange={(e) => setWeight(parseFloat(e.currentTarget.value))}
            style={{ width: "100%" }}
          />
          <s-text tone="subdued" size="small">
            How much this competitor’s prices influence weighted stats. 0 = ignored, 1 = full weight.
          </s-text>
        </s-stack>

        <s-stack direction="inline" gap="base">
          <s-button
            variant="primary"
            disabled={!dirty || saving ? true : undefined}
            onClick={onSave}
          >
            {saving ? "Saving…" : dirty ? "Save changes" : "Saved"}
          </s-button>
        </s-stack>
      </s-stack>
    </s-section>
  );
}

export default function CompetitorsPage() {
  const { competitors } = useLoaderData();

  const summary = useMemo(() => {
    const enabled = competitors.filter((c) => c.enabled).length;
    const totalObs = competitors.reduce((s, c) => s + (c.observationCount || 0), 0);
    return { total: competitors.length, enabled, totalObs };
  }, [competitors]);

  return (
    <s-page
      heading="Competitors"
      subheading="Set how much each competitor’s prices count toward your pricing decisions."
    >
      <s-stack direction="block" gap="loose">
        <s-section>
          <s-text>
            {summary.enabled} of {summary.total} competitor
            {summary.total === 1 ? "" : "s"} enabled · {summary.totalObs} total price
            observation{summary.totalObs === 1 ? "" : "s"} recorded.
          </s-text>
        </s-section>

        {competitors.length === 0 ? (
          <s-section>
            <s-text tone="subdued">
              No competitors yet. Add a scraping config in the Controller and once
              the first scrape lands, the competitor will show up here.
            </s-text>
          </s-section>
        ) : (
          competitors.map((c) => <CompetitorRow key={c.id} competitor={c} />)
        )}
      </s-stack>
    </s-page>
  );
}

export function ErrorBoundary() {
  return boundary.error(useRouteError());
}

export const headers = (headersArgs) => boundary.headers(headersArgs);
