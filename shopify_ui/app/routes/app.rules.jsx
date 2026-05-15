/* eslint-disable react/prop-types */
import { useState } from "react";
import { useFetcher, useLoaderData, useRouteError } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

const SCOPES = ["SHOP", "PRODUCT", "VARIANT"];
const RULE_TYPES = [
  { value: "MATCH_LOWEST",        label: "Match lowest competitor" },
  { value: "BEAT_BY_PCT",         label: "Beat competitor by %" },
  { value: "STAY_ABOVE_COST_PCT", label: "Stay above cost by %" },
  { value: "MATCH_TIER_LOWEST",   label: "Match lowest in tier(s)" },
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

  return {
    shop,
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

  if (intent === "create") {
    const scope = String(fd.get("scope") || "SHOP");
    const scopeRef = scope === "SHOP" ? null : (String(fd.get("scopeRef") || "").trim() || null);
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
        maxDailyDeltaPct:    toNum("maxDailyDeltaPct") ?? 20,
        tierFilter,
        maxStalenessSeconds: Number(fd.get("maxStalenessSeconds") || 86400),
        autoApply:           fd.get("autoApply") === "true",
        priority:            Number(fd.get("priority") || 100),
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
  const { rules } = useLoaderData();
  return (
    <s-page heading="Pricing Rules">
      <s-section heading="Existing rules">
        {rules.length === 0 ? (
          <s-text>No rules yet. Create one below to enable pricing decisions.</s-text>
        ) : (
          <s-stack gap="base">
            {rules.map((r) => <RuleCard key={r.id} rule={r} />)}
          </s-stack>
        )}
      </s-section>

      <s-section heading="Create new rule">
        <CreateRuleForm />
      </s-section>
    </s-page>
  );
}

function RuleCard({ rule }) {
  const fetcher = useFetcher();
  const ruleTypeLabel =
    RULE_TYPES.find((t) => t.value === rule.ruleType)?.label || rule.ruleType;

  return (
    <s-section>
      <s-stack direction="inline" gap="base" alignment="space-between">
        <s-stack gap="tight">
          <s-text emphasis="bold">{ruleTypeLabel}</s-text>
          <s-text tone="subdued">
            scope: {rule.scope}{rule.scopeRef ? ` (${rule.scopeRef})` : ""}
            {" • "}priority: {rule.priority}
            {" • "}decisions logged: {rule.decisionCount}
          </s-text>
          <s-text tone="subdued">
            {rule.params?.pct != null ? `pct=${rule.params.pct}  ·  ` : ""}
            floor: {rule.floorPrice ?? "—"}  ·  ceiling: {rule.ceilingPrice ?? "—"}  ·
            maxΔ/day: {rule.maxDailyDeltaPct ?? "—"}%  ·
            stale-after: {rule.maxStalenessSeconds}s
          </s-text>
          {rule.tierFilter?.length ? (
            <s-text tone="subdued">tiers: {rule.tierFilter.join(", ")}</s-text>
          ) : null}
        </s-stack>

        <s-stack gap="tight">
          <fetcher.Form method="post">
            <input type="hidden" name="intent" value="toggleEnabled" />
            <input type="hidden" name="id" value={rule.id} />
            <input type="hidden" name="enabled" value={String(!rule.enabled)} />
            <s-button type="submit" variant={rule.enabled ? "secondary" : "primary"}>
              {rule.enabled ? "Disable" : "Enable"}
            </s-button>
          </fetcher.Form>

          <fetcher.Form method="post">
            <input type="hidden" name="intent" value="toggleAutoApply" />
            <input type="hidden" name="id" value={rule.id} />
            <input type="hidden" name="autoApply" value={String(!rule.autoApply)} />
            <s-button type="submit" variant={rule.autoApply ? "primary" : "secondary"}>
              {rule.autoApply ? "Auto-apply ON" : "Auto-apply OFF"}
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

function CreateRuleForm() {
  const fetcher = useFetcher();
  const [scope, setScope] = useState("SHOP");
  const [ruleType, setRuleType] = useState("MATCH_LOWEST");

  const needsPct = ruleType === "BEAT_BY_PCT" || ruleType === "STAY_ABOVE_COST_PCT";
  const needsTiers = ruleType === "MATCH_TIER_LOWEST";

  return (
    <fetcher.Form method="post">
      <input type="hidden" name="intent" value="create" />

      <s-stack gap="base">
        <s-select
          name="scope"
          label="Scope"
          value={scope}
          onChange={(e) => setScope(e.target.value)}
        >
          {SCOPES.map((s) => <option key={s} value={s}>{s}</option>)}
        </s-select>

        {scope !== "SHOP" && (
          <s-text-field
            name="scopeRef"
            label={scope === "PRODUCT" ? "Shopify product ID (gid)" : "Shopify variant ID (gid)"}
            placeholder="gid://shopify/Product/..."
          />
        )}

        <s-select
          name="ruleType"
          label="Rule type"
          value={ruleType}
          onChange={(e) => setRuleType(e.target.value)}
        >
          {RULE_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
        </s-select>

        {needsPct && (
          <s-text-field
            name="pct"
            type="number"
            label={ruleType === "BEAT_BY_PCT" ? "Beat competitor by (%)" : "Margin above cost (%)"}
            placeholder={ruleType === "BEAT_BY_PCT" ? "5" : "30"}
          />
        )}

        {needsTiers && (
          <s-stack gap="tight">
            <s-text>Competitor tiers to include</s-text>
            {TIERS.map((t) => (
              <s-checkbox key={t} name={`tier_${t}`} value="true" label={t} />
            ))}
          </s-stack>
        )}

        <s-text-field
          name="floorPrice"
          type="number"
          label="Floor price (never go below)"
          placeholder="0"
        />
        <s-text-field
          name="ceilingPrice"
          type="number"
          label="Ceiling price (never go above)"
          placeholder="100000"
        />
        <s-text-field
          name="maxDailyDeltaPct"
          type="number"
          label="Max daily price change (%)"
          placeholder="20"
        />
        <s-text-field
          name="maxStalenessSeconds"
          type="number"
          label="Reject stats older than (seconds)"
          placeholder="86400"
        />
        <s-text-field
          name="priority"
          type="number"
          label="Priority (higher wins within same scope)"
          placeholder="100"
        />
        <s-select name="charm" label="Charm rounding">
          <option value="">2 decimal places</option>
          <option value="ninety_nine">.99 ending</option>
        </s-select>
        <s-checkbox name="autoApply" value="true" label="Auto-apply to Shopify (requires CONFIRMED match)" />

        <s-button type="submit" variant="primary">Create rule</s-button>
      </s-stack>
    </fetcher.Form>
  );
}

export function ErrorBoundary() {
  return boundary.error(useRouteError());
}
