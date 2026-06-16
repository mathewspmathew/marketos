/* eslint-disable react/prop-types */
import { useState } from "react";
import { useFetcher, useLoaderData, useRouteError } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

const RULE_TYPES = [
  { value: "MATCH_LOWEST",        label: "Match the lowest competitor price" },
  { value: "BEAT_BY_PCT",         label: "Undercut the lowest competitor by a %" },
  { value: "STAY_ABOVE_COST_PCT", label: "Always keep margin above cost (no competitor reference)" },
  { value: "MATCH_TIER_LOWEST",   label: "Match the lowest in chosen tiers only" },
];
const TIERS = ["PREMIUM", "MIDMARKET", "BUDGET"];

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;

  const rules = await db.pricingRule.findMany({
    where: { shopDomain: shop },
    orderBy: [{ enabled: "desc" }, { priority: "desc" }, { createdAt: "desc" }],
  });

  // Decision counts per rule for the "is this rule actually firing?" hint.
  const counts = await db.priceDecision.groupBy({
    by: ["ruleId"],
    where: { shopDomain: shop, ruleId: { not: null } },
    _count: { _all: true },
  });
  const countMap = Object.fromEntries(counts.map((r) => [r.ruleId, r._count._all]));

  // Products list for the per-product picker (replaces raw GID input).
  // Only show products that have at least one variant — variant-less products
  // can't be priced anyway.
  const products = await db.shopifyProduct.findMany({
    where: { shopDomain: shop, ShopifyVariant: { some: {} } },
    orderBy: { title: "asc" },
    select: { id: true, title: true, vendor: true, imageUrl: true },
    take: 500,
  });

  return {
    shop,
    products,
    rules: rules.map((r) => ({
      ...r,
      floorPrice:       r.floorPrice       != null ? Number(r.floorPrice)       : null,
      ceilingPrice:     r.ceilingPrice     != null ? Number(r.ceilingPrice)     : null,
      maxDailyDeltaPct: r.maxDailyDeltaPct != null ? Number(r.maxDailyDeltaPct) : null,
      mlBlendWeight:    Number(r.mlBlendWeight),
      decisionCount:    countMap[r.id] ?? 0,
    })),
  };
};

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const fd = await request.formData();
  const intent = fd.get("intent");

  // One-click "get started" rule: shop-wide, match lowest, auto-apply on,
  // conservative 5% daily delta. Idempotent — won't create duplicates.
  if (intent === "createDefault") {
    const existing = await db.pricingRule.findFirst({
      where: { shopDomain: shop, scope: "SHOP", ruleType: "MATCH_LOWEST" },
    });
    if (existing) {
      await db.pricingRule.update({
        where: { id: existing.id },
        data:  { enabled: true, autoApply: true },
      });
      return { ok: true, intent };
    }
    await db.pricingRule.create({
      data: {
        shopDomain:          shop,
        scope:               "SHOP",
        scopeRef:            null,
        ruleType:            "MATCH_LOWEST",
        params:              { charm: "ninety_nine" },
        floorPrice:          null,
        ceilingPrice:        null,
        maxDailyDeltaPct:    5,
        tierFilter:          [],
        maxStalenessSeconds: 86400,
        autoApply:           true,
        priority:            10,
        enabled:             true,
      },
    });
    return { ok: true, intent };
  }

  if (intent === "create") {
    const scope = String(fd.get("scope") || "SHOP");
    const scopeRef = scope === "SHOP" ? null : (String(fd.get("scopeRef") || "").trim() || null);
    if (scope !== "SHOP" && !scopeRef) {
      return { ok: false, error: "missing_product" };
    }
    const ruleType = String(fd.get("ruleType") || "MATCH_LOWEST");
    const pct = fd.get("pct");
    const charm = String(fd.get("charm") || "");
    const params = {};
    if (ruleType === "BEAT_BY_PCT" || ruleType === "STAY_ABOVE_COST_PCT") {
      params.pct = pct != null && pct !== "" ? Number(pct) : 0;
    }
    if (charm === "ninety_nine") params.charm = "ninety_nine";

    const tierFilter = TIERS.filter((t) => fd.get(`tier_${t}`) === "true");

    const toNum = (key) => {
      const v = fd.get(key);
      if (v == null || v === "") return null;
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    };

    await db.pricingRule.create({
      data: {
        shopDomain:          shop,
        scope,
        scopeRef,
        ruleType,
        params,
        floorPrice:          toNum("floorPrice"),
        ceilingPrice:        toNum("ceilingPrice"),
        maxDailyDeltaPct:    toNum("maxDailyDeltaPct") ?? 5,
        tierFilter,
        maxStalenessSeconds: 86400,
        autoApply:           fd.get("autoApply") === "true",
        priority:            scope === "VARIANT" ? 300 : scope === "PRODUCT" ? 200 : 100,
        enabled:             true,
      },
    });
    return { ok: true };
  }

  if (intent === "toggleEnabled") {
    const id = String(fd.get("id"));
    const enabled = fd.get("enabled") === "true";
    await db.pricingRule.update({ where: { id }, data: { enabled } });
    return { ok: true };
  }

  if (intent === "toggleAutoApply") {
    const id = String(fd.get("id"));
    const autoApply = fd.get("autoApply") === "true";
    await db.pricingRule.update({ where: { id }, data: { autoApply } });
    return { ok: true };
  }

  if (intent === "delete") {
    const id = String(fd.get("id"));
    await db.pricingRule.delete({ where: { id } });
    return { ok: true };
  }

  return { ok: false, error: "unknown_intent" };
};

export default function RulesPage() {
  const { rules, products } = useLoaderData();
  const hasShopWide = rules.some((r) => r.scope === "SHOP" && r.enabled);

  return (
    <s-page heading="Pricing rules">
      <s-section>
        <s-text>
          A rule tells the engine <em>how</em> to set prices once it sees
          competitors. Without at least one rule, no prices change.
        </s-text>
      </s-section>

      {!hasShopWide ? <GetStartedCard /> : null}

      <s-section heading={`Your rules (${rules.length})`}>
        {rules.length === 0 ? (
          <s-text tone="subdued">No rules yet. Use the Get started card above, or build a custom one below.</s-text>
        ) : (
          <s-stack gap="base">
            {rules.map((r) => <RuleCard key={r.id} rule={r} products={products} />)}
          </s-stack>
        )}
      </s-section>

      <s-section heading="Build a custom rule">
        <CreateRuleForm products={products} />
      </s-section>
    </s-page>
  );
}

function GetStartedCard() {
  const fetcher = useFetcher();
  const busy = fetcher.state !== "idle";
  return (
    <s-section>
      <s-stack gap="tight" style={{
        padding: 12, borderRadius: 8, background: "#eef5ff",
        border: "1px solid #d3e3fd",
      }}>
        <s-text emphasis="bold">Get started in one click</s-text>
        <s-text>
          Creates one shop-wide rule: <strong>match the lowest competitor price</strong>,
          auto-apply to Shopify, and never move a price by more than <strong>5%</strong> in
          a day. Applies to every product that has a confirmed competitor match
          and auto-pricing turned on. You can fine-tune or delete it any time.
        </s-text>
        <fetcher.Form method="post">
          <input type="hidden" name="intent" value="createDefault" />
          <s-button type="submit" variant="primary" disabled={busy}>
            {busy ? "Creating…" : "Create default rule"}
          </s-button>
        </fetcher.Form>
      </s-stack>
    </s-section>
  );
}

function ruleScopeLabel(r, products) {
  if (r.scope === "SHOP")    return "All products in this shop";
  if (r.scope === "PRODUCT") {
    const p = products.find((x) => x.id === r.scopeRef);
    return p ? `Product: ${p.title}` : `Product: ${r.scopeRef}`;
  }
  if (r.scope === "VARIANT") return `Variant: ${r.scopeRef}`;
  return r.scope;
}

function ruleSummary(r) {
  if (r.ruleType === "MATCH_LOWEST")        return "Match the lowest competitor price.";
  if (r.ruleType === "BEAT_BY_PCT")         return `Undercut the lowest competitor by ${r.params?.pct ?? 0}%.`;
  if (r.ruleType === "STAY_ABOVE_COST_PCT") return `Keep at least ${r.params?.pct ?? 0}% margin above cost.`;
  if (r.ruleType === "MATCH_TIER_LOWEST")   return `Match the lowest competitor in tiers: ${(r.tierFilter || []).join(", ") || "(none)"}.`;
  return r.ruleType;
}

function RuleCard({ rule, products }) {
  const fetcher = useFetcher();

  return (
    <s-section>
      <s-stack direction="inline" gap="base" alignment="space-between">
        <s-stack gap="tight" style={{ flex: 2 }}>
          <s-text emphasis="bold">{ruleSummary(rule)}</s-text>
          <s-text tone="subdued">{ruleScopeLabel(rule, products)}</s-text>
          <s-text tone="subdued" style={{ fontSize: 12 }}>
            Max daily change: <strong>{rule.maxDailyDeltaPct ?? "—"}%</strong>
            {rule.floorPrice != null    ? ` · floor ₹${rule.floorPrice}` : ""}
            {rule.ceilingPrice != null  ? ` · ceiling ₹${rule.ceilingPrice}` : ""}
            {" · "}decisions logged: <strong>{rule.decisionCount}</strong>
            {!rule.enabled ? " · DISABLED" : ""}
          </s-text>
        </s-stack>

        <s-stack gap="tight" alignment="end" style={{ flex: 1 }}>
          <fetcher.Form method="post">
            <input type="hidden" name="intent" value="toggleAutoApply" />
            <input type="hidden" name="id" value={rule.id} />
            <input type="hidden" name="autoApply" value={String(!rule.autoApply)} />
            <s-button type="submit" variant={rule.autoApply ? "primary" : "secondary"}>
              {rule.autoApply ? "Pushes to Shopify: ON" : "Suggestions only"}
            </s-button>
          </fetcher.Form>

          <fetcher.Form method="post">
            <input type="hidden" name="intent" value="toggleEnabled" />
            <input type="hidden" name="id" value={rule.id} />
            <input type="hidden" name="enabled" value={String(!rule.enabled)} />
            <s-button type="submit" variant={rule.enabled ? "secondary" : "primary"}>
              {rule.enabled ? "Disable rule" : "Enable rule"}
            </s-button>
          </fetcher.Form>

          <fetcher.Form method="post">
            <input type="hidden" name="intent" value="delete" />
            <input type="hidden" name="id" value={rule.id} />
            <s-button type="submit" tone="critical" variant="secondary">Delete</s-button>
          </fetcher.Form>
        </s-stack>
      </s-stack>
    </s-section>
  );
}

function CreateRuleForm({ products }) {
  const fetcher = useFetcher();
  const [scope, setScope]       = useState("SHOP");
  const [ruleType, setRuleType] = useState("MATCH_LOWEST");
  const [showAdvanced, setShowAdvanced] = useState(false);

  const needsPct   = ruleType === "BEAT_BY_PCT" || ruleType === "STAY_ABOVE_COST_PCT";
  const needsTiers = ruleType === "MATCH_TIER_LOWEST";

  return (
    <fetcher.Form method="post">
      <input type="hidden" name="intent" value="create" />

      <s-stack gap="base">
        <s-select
          name="scope"
          label="Apply this rule to"
          value={scope}
          onChange={(e) => setScope(e.target.value)}
        >
          <option value="SHOP">All my products</option>
          <option value="PRODUCT">A specific product</option>
        </s-select>

        {scope === "PRODUCT" && (
          <s-select name="scopeRef" label="Which product">
            <option value="">— pick a product —</option>
            {products.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}{p.vendor ? ` · ${p.vendor}` : ""}
              </option>
            ))}
          </s-select>
        )}

        <s-select
          name="ruleType"
          label="Pricing strategy"
          value={ruleType}
          onChange={(e) => setRuleType(e.target.value)}
        >
          {RULE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
        </s-select>

        {needsPct && (
          <s-text-field
            name="pct"
            type="number"
            label={ruleType === "BEAT_BY_PCT" ? "Undercut by (%)" : "Margin above cost (%)"}
            placeholder={ruleType === "BEAT_BY_PCT" ? "5" : "30"}
          />
        )}

        {needsTiers && (
          <s-stack gap="tight">
            <s-text>Which competitor tiers should I consider?</s-text>
            {TIERS.map((t) => (
              <s-checkbox key={t} name={`tier_${t}`} value="true" label={t} />
            ))}
          </s-stack>
        )}

        <s-text-field
          name="maxDailyDeltaPct"
          type="number"
          label="Max change per day (%)"
          placeholder="5"
        />

        <s-checkbox name="autoApply" value="true"
                    label="Push approved prices to Shopify automatically" />

        <s-button type="button" variant="secondary"
                  onClick={() => setShowAdvanced(!showAdvanced)}>
          {showAdvanced ? "Hide advanced options ▲" : "Show advanced options ▼"}
        </s-button>

        {showAdvanced && (
          <s-stack gap="base" style={{
            padding: 12, borderRadius: 6, background: "#fafbfb",
            border: "1px solid #e4e5e7",
          }}>
            <s-text-field name="floorPrice" type="number"
              label="Floor — never go below this price" placeholder="(none)" />
            <s-text-field name="ceilingPrice" type="number"
              label="Ceiling — never go above this price" placeholder="(none)" />
            <s-select name="charm" label="Price ending">
              <option value="">Two decimal places (₹24.97)</option>
              <option value="ninety_nine">.99 ending (₹24.99)</option>
            </s-select>
          </s-stack>
        )}

        <s-button type="submit" variant="primary">Create rule</s-button>
      </s-stack>
    </fetcher.Form>
  );
}

export function ErrorBoundary() {
  return boundary.error(useRouteError());
}
