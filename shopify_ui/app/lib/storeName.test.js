import { describe, it, expect } from "vitest";
import { deriveStoreName } from "./storeName";

describe("deriveStoreName", () => {
  it("strips .myshopify.com and title-cases hyphen-separated words", () => {
    expect(deriveStoreName("marketos-buzz.myshopify.com")).toBe("Marketos Buzz");
  });

  it("title-cases dot-separated words", () => {
    expect(deriveStoreName("acme.corp.myshopify.com")).toBe("Acme Corp");
  });

  it("handles a single-word domain", () => {
    expect(deriveStoreName("stationeryshop.myshopify.com")).toBe("Stationeryshop");
  });

  it("returns the raw domain unchanged if it has no .myshopify.com suffix", () => {
    expect(deriveStoreName("custom-domain.com")).toBe("custom-domain.com");
  });
});
