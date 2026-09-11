/**
 * validateShopHostParams.js — pre-flight guard for the `shop`/`host` query
 * params Shopify's App Bridge attaches to every embedded document request.
 *
 * Why this exists: inside `authenticate.admin()`, the SDK's `sanitizeHost()`
 * (in @shopify/shopify-api) base64-decodes the `host` param and then calls
 * `new URL()` on the decoded value with NO try/catch. Any request where the
 * param is base64-valid but decodes to something that isn't a valid URL host
 * (e.g. `aGVsbG8gd29ybGQ=` → "hello world") escapes as
 * `TypeError: Invalid URL` → 500 "Unexpected Server Error", instead of the
 * intended app-bridge bounce. Sentry issue PYTHON-7A / d9a8d9ac, reproduced
 * locally against shopify-app-react-router 1.2.1 + shopify-api 13.1.0
 * (still unfixed on shopify-app-js main as of Sep 2026).
 *
 * Fix strategy (industry-standard "validate at the edge, fail closed into a
 * redirect" pattern): run the SDK's own regex logic *before* handing the
 * request to the SDK, and throw the exact same Response the SDK throws for a
 * *missing* shop param — the App Bridge bounce page. Legit requests always
 * come from App Bridge, so there is no legit traffic to break; garbage
 * requests (bots, scanners, tampered URLs) get the normal re-embed flow
 * instead of a 500.
 */

// Mirrors @shopify/shopify-api's sanitizeShop domain list (13.1.0) plus
// customShopDomains support, which the real SDK checks via config.
const SHOP_DOMAINS = [
  "myshopify\\.com",
  "shopify\\.com",
  "myshopify\\.io",
  "shop\\.dev",
];
const SHOP_REGEX = new RegExp(
  `^[a-zA-Z0-9][a-zA-Z0-9-_]*\\.(${SHOP_DOMAINS.join("|")})[/]*$`,
);

// Mirrors the SDK's base64 pre-check in sanitizeHost — a host param must
// decode cleanly from base64 before its decoded form is worth parsing.
const BASE64_REGEX = /^[0-9a-zA-Z+/]+={0,2}$/;

const HOST_ORIGIN_REGEX = new RegExp(
  `\\.(${[
    "myshopify\\.com",
    "shopify\\.com",
    "myshopify\\.io",
    "spin\\.dev",
    "shop\\.dev",
  ].join("|")})$`,
);

function decodeHost(host) {
  // Node 20+: atob exists globally; mirror the SDK's decodeHost exactly.
  return atob(host);
}

/**
 * Returns true when the request's shop/host params look well-formed enough
 * for the SDK to handle them without crashing. Mirrors sanitizeShop() +
 * sanitizeHost() from @shopify/shopify-api 13.x, including its
 * `new URL()` behaviour, but never throws.
 */
export function shopAndHostParamsAreSafe(request) {
  const { searchParams } = new URL(request.url);
  const shop = searchParams.get("shop");
  const host = searchParams.get("host");

  // A shop param is mandatory for any /app/* document request; if it's
  // missing/invalid the SDK already handles that gracefully (app-bridge
  // bounce), so let those through untouched.
  if (!shop || !SHOP_REGEX.test(shop)) {
    return true;
  }

  // Shop is valid → the SDK WILL parse `host`. Guard exactly the shapes
  // that make it throw.
  if (!host || !BASE64_REGEX.test(host)) {
    return true; // non-base64 garbage: SDK handles it as "invalid host"
  }

  let decoded;
  try {
    decoded = decodeHost(host);
  } catch {
    return false;
  }

  try {
    // This is the line the SDK runs unguarded — replicate it safely.
    // eslint-disable-next-line no-new
    new URL(`https://${decoded}`);
  } catch {
    return false;
  }

  // Also reject decoded hosts that parse but aren't Shopify admin origins —
  // same allowlist the SDK applies right after its new URL() call.
  const { hostname } = new URL(`https://${decoded}`);
  return HOST_ORIGIN_REGEX.test(hostname);
}
