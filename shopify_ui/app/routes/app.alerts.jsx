/* eslint-disable react/prop-types */
import { useFetcher, useLoaderData, useRouteError, useSearchParams, Link } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";
import db from "../db.server";

// Friendly explanations for the alert codes that get stored in
// PriceDecision.alertReason. Keep keys in sync with services/shopify_svc/main.py.
const ALERT_EXPLAIN = {
  MISSING_OFFLINE_TOKEN: {
    title: "Shopify lost permission to write prices",
    body:  "We need a fresh authorization to push price updates. Please reopen the app from your Shopify admin to renew access.",
  },
  SHOPIFY_WRITE_FAILED: {
    title: "Shopify rejected our last price update",
    body:  "Network or server error while pushing the new price. We'll retry automatically — if it keeps failing, check Shopify's status page.",
  },
  SHOPIFY_USER_ERROR: {
    title: "Shopify refused the price change",
    body:  "Shopify returned a validation error (e.g. price below allowed range, or variant constraints). See details below.",
  },
};

function explainAlert(code) {
  return ALERT_EXPLAIN[code] || { title: code, body: "(No explanation available for this alert code yet.)" };
}

const SEVERITY_FILTERS = [
  { key: "all",      label: "All" },
  { key: "CRITICAL", label: "Critical" },
  { key: "WARN",     label: "Warning" },
  { key: "INFO",     label: "Info" },
];

const STATE_FILTERS = [
  { key: "open",      label: "Open" },
  { key: "dismissed", label: "Dismissed" },
];

export const loader = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const url = new URL(request.url);
  const severity = url.searchParams.get("severity") || "all";
  const state    = url.searchParams.get("state")    || "open";

  // Alerts are sourced from PriceDecision rows where alertReason is set
  // (PricingAlert was folded in). Type ↔ alertReason, severity ↔ alertSeverity,
  // payload ↔ shopifyResponse, createdAt ↔ decidedAt, dismissedAt ↔ alertDismissedAt.
  const where = { shopDomain: shop, alertReason: { not: null } };
  if (severity !== "all") where.alertSeverity = severity;
  if (state === "open")      where.alertDismissedAt = null;
  if (state === "dismissed") where.alertDismissedAt = { not: null };

  const alerts = await db.priceDecision.findMany({
    where,
    orderBy: { decidedAt: "desc" },
    take: 100,
    include: {
      variant: {
        select: {
          id: true, title: true,
          product: { select: { title: true } },
        },
      },
    },
  });

  const baseAlertWhere = { shopDomain: shop, alertReason: { not: null } };
  const allOpen = await db.priceDecision.count({
    where: { ...baseAlertWhere, alertDismissedAt: null },
  });
  const allDismissed = await db.priceDecision.count({
    where: { ...baseAlertWhere, alertDismissedAt: { not: null } },
  });
  const bySeverity = await db.priceDecision.groupBy({
    by: ["alertSeverity"],
    where: { ...baseAlertWhere, alertDismissedAt: null },
    _count: { _all: true },
  });
  const sevMap = Object.fromEntries(bySeverity.map((r) => [r.alertSeverity, r._count._all]));

  return {
    shop,
    severity,
    state,
    alerts: alerts.map((a) => ({
      id:          a.id,
      type:        a.alertReason,
      severity:    a.alertSeverity,
      payload:     a.shopifyResponse,
      createdAt:   a.decidedAt,
      dismissedAt: a.alertDismissedAt,
      variant:     a.variant ? {
        id:           a.variant.id,
        title:        a.variant.title,
        productTitle: a.variant.product.title,
      } : null,
    })),
    counts: {
      open:      allOpen,
      dismissed: allDismissed,
      CRITICAL:  sevMap.CRITICAL || 0,
      WARN:      sevMap.WARN     || 0,
      INFO:      sevMap.INFO     || 0,
    },
  };
};

export const action = async ({ request }) => {
  const { session } = await authenticate.admin(request);
  const shop = session.shop;
  const fd = await request.formData();
  const intent = fd.get("intent");
  const id = String(fd.get("id") || "");
  if (!id) return { ok: false, error: "missing_id" };

  if (intent === "dismiss") {
    await db.priceDecision.updateMany({
      where: { id, shopDomain: shop },
      data:  { alertDismissedAt: new Date() },
    });
    return { ok: true };
  }
  if (intent === "undismiss") {
    await db.priceDecision.updateMany({
      where: { id, shopDomain: shop },
      data:  { alertDismissedAt: null },
    });
    return { ok: true };
  }
  if (intent === "dismissAll") {
    await db.priceDecision.updateMany({
      where: { shopDomain: shop, alertReason: { not: null }, alertDismissedAt: null },
      data:  { alertDismissedAt: new Date() },
    });
    return { ok: true };
  }
  return { ok: false, error: "unknown_intent" };
};

export default function AlertsPage() {
  const { alerts, severity, state, counts } = useLoaderData();
  const [searchParams, setSearchParams] = useSearchParams();
  const fetcher = useFetcher();

  function setFilter(key, value) {
    const next = new URLSearchParams(searchParams);
    next.set(key, value);
    setSearchParams(next);
  }

  return (
    <s-page heading="Alerts">
      <s-section>
        <s-stack direction="inline" gap="tight">
          {STATE_FILTERS.map((f) => (
            <s-button
              key={f.key}
              variant={state === f.key ? "primary" : "secondary"}
              onClick={() => setFilter("state", f.key)}
            >
              {f.label} ({counts[f.key] ?? 0})
            </s-button>
          ))}
          <span style={{ width: 16 }} />
          {SEVERITY_FILTERS.map((f) => (
            <s-button
              key={f.key}
              variant={severity === f.key ? "primary" : "secondary"}
              onClick={() => setFilter("severity", f.key)}
            >
              {f.label}{f.key !== "all" ? ` (${counts[f.key] ?? 0})` : ""}
            </s-button>
          ))}
          <span style={{ flex: 1 }} />
          {state === "open" && counts.open > 0 && (
            <fetcher.Form method="post">
              <input type="hidden" name="intent" value="dismissAll" />
              <input type="hidden" name="id" value="all" />
              <s-button type="submit" variant="secondary">Dismiss all</s-button>
            </fetcher.Form>
          )}
        </s-stack>
      </s-section>

      {alerts.length === 0 ? (
        <s-section>
          <s-text tone="subdued">No alerts in this view.</s-text>
        </s-section>
      ) : (
        <s-stack gap="tight">
          {alerts.map((a) => <AlertCard key={a.id} alert={a} />)}
        </s-stack>
      )}
    </s-page>
  );
}

function AlertCard({ alert }) {
  const fetcher = useFetcher();
  const isCritical = alert.severity === "CRITICAL";
  const isWarn     = alert.severity === "WARN";
  const bg  = isCritical ? "#fff5f5" : isWarn ? "#fff8e8" : "#f6faff";
  const dot = isCritical ? "#bf0711" : isWarn ? "#b9852c" : "#1f6feb";
  const explain = explainAlert(alert.type);

  return (
    <s-section>
      <s-stack direction="inline" gap="base" alignment="space-between"
        style={{ background: bg, padding: 12, borderRadius: 6 }}>
        <s-stack gap="tight" style={{ flex: 1 }}>
          <s-stack direction="inline" gap="tight">
            <span style={{
              display: "inline-block", width: 8, height: 8,
              borderRadius: 999, background: dot, marginTop: 6,
            }} />
            <s-text emphasis="bold">{explain.title}</s-text>
            <s-text tone="subdued">· {alert.severity}</s-text>
            <s-text tone="subdued">· {new Date(alert.createdAt).toLocaleString()}</s-text>
          </s-stack>

          <s-text>{explain.body}</s-text>

          {alert.variant && (
            <s-text tone="subdued" style={{ fontSize: 12 }}>
              Affects:{" "}
              <Link to={`/app/pricing/${encodeURIComponent(alert.variant.id)}`}>
                {alert.variant.productTitle} — {alert.variant.title}
              </Link>
            </s-text>
          )}

          <s-text tone="subdued" style={{ fontSize: 11 }}>
            Alert code: <span style={{ fontFamily: "monospace" }}>{alert.type}</span>
          </s-text>

          {alert.payload && Object.keys(alert.payload).length > 0 && (
            <details>
              <summary style={{ cursor: "pointer", fontSize: 12, color: "#6d7175" }}>
                Technical details
              </summary>
              <pre style={{
                fontFamily: "monospace", fontSize: 11, marginTop: 6,
                background: "#fff", padding: 8, borderRadius: 4,
                overflow: "auto", maxHeight: 200,
              }}>
                {JSON.stringify(alert.payload, null, 2)}
              </pre>
            </details>
          )}
        </s-stack>

        <fetcher.Form method="post">
          <input type="hidden"
                 name="intent"
                 value={alert.dismissedAt ? "undismiss" : "dismiss"} />
          <input type="hidden" name="id" value={alert.id} />
          <s-button type="submit" variant="secondary">
            {alert.dismissedAt ? "Restore" : "Dismiss"}
          </s-button>
        </fetcher.Form>
      </s-stack>
    </s-section>
  );
}

export function ErrorBoundary() {
  return boundary.error(useRouteError());
}
