import { describe, it, expect } from "vitest";
import { shopAndHostParamsAreSafe } from "./validateShopHostParams";

function req(url) {
  return new Request(url);
}

const SH = "shop=acme.myshopify.com";
const GOOD_HOST = "host=YWNtZS5teXNob3BpZnkuY29t"; // "acme.myshopify.com"

describe("shopAndHostParamsAreSafe", () => {
  it("lets requests with no shop param through (SDK handles the bounce)", () => {
    expect(shopAndHostParamsAreSafe(req("http://x/app"))).toBe(true);
  });

  it("lets requests with an invalid shop param through (SDK bounce)", () => {
    expect(shopAndHostParamsAreSafe(req("http://x/app?shop=%%%"))).toBe(true);
    expect(shopAndHostParamsAreSafe(req("http://x/app?shop=evil.com"))).toBe(true);
  });

  it("lets a valid shop + valid base64 Shopify host through", () => {
    expect(
      shopAndHostParamsAreSafe(req(`http://x/app?${SH}&${GOOD_HOST}&embedded=1`)),
    ).toBe(true);
  });

  it("lets a valid shop + non-base64 host through (SDK handles it)", () => {
    expect(shopAndHostParamsAreSafe(req(`http://x/app?${SH}&host=%%%£££`))).toBe(true);
  });

  it("lets a valid shop + missing host through (SDK handles it)", () => {
    expect(shopAndHostParamsAreSafe(req(`http://x/app?${SH}`))).toBe(true);
  });

  // The actual Sentry PYTHON-7A crash: base64-valid host that decodes to a
  // non-URL — the SDK's unguarded new URL() throws TypeError: Invalid URL.
  it("flags base64-valid host that decodes to a non-URL ('hello world')", () => {
    expect(
      shopAndHostParamsAreSafe(req(`http://x/app?${SH}&host=aGVsbG8gd29ybGQ=`)),
    ).toBe(false);
  });

  it("flags base64-valid host that decodes to a non-Shopify origin", () => {
    // "evil.example.com"
    expect(
      shopAndHostParamsAreSafe(req(`http://x/app?${SH}&host=ZXZpbC5leGFtcGxlLmNvbQ==`)),
    ).toBe(false);
  });

  it("lets an empty base64 host through (SDK regex rejects empty, handles it)", () => {
    // The SDK's base64regex requires ≥1 char, so '' fails the check and
    // sanitizeHost returns null gracefully — no crash to guard against.
    expect(shopAndHostParamsAreSafe(req(`http://x/app?${SH}&host=`))).toBe(true);
  });

  it("flags a base64 host decoding to a URL-host with bogus TLD", () => {
    // "acme.notshopify.com" — parses fine but fails the origin allowlist
    expect(
      shopAndHostParamsAreSafe(req(`http://x/app?${SH}&host=YWNtZS5ub3RzaG9waWZ5LmNvbQ==`)),
    ).toBe(false);
  });

  it("accepts spin.dev / shop.dev / myshopify.io hosts (SDK allowlist)", () => {
    for (const domain of ["acme.spin.dev", "acme.shop.dev", "acme.myshopify.io"]) {
      const encoded = Buffer.from(domain).toString("base64");
      expect(shopAndHostParamsAreSafe(req(`http://x/app?${SH}&host=${encoded}`))).toBe(
        true,
      );
    }
  });
});
