// Derives a merchant-friendly display name from a shop domain, avoiding an
// extra Shopify Admin API call for the real shop display name — used only
// in notification-email copy, not shown anywhere else in the app.
export function deriveStoreName(shopDomain) {
  const suffix = ".myshopify.com";
  if (!shopDomain || !shopDomain.endsWith(suffix)) {
    return shopDomain ?? "";
  }
  const base = shopDomain.slice(0, -suffix.length);
  return base
    .split(/[-.]/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}
