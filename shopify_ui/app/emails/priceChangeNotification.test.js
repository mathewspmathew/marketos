import { describe, it, expect } from "vitest";
import { renderPriceChangeEmail } from "./priceChangeNotification.server";

describe("renderPriceChangeEmail", () => {
  it("includes store name, product title, and every variant's old/new price", async () => {
    const { subject, html } = await renderPriceChangeEmail({
      storeName: "Marketos Buzz",
      productTitle: "Classic Notebook",
      currency: "INR",
      variants: [
        { variantTitle: "A5 / Ruled", oldPrice: "199.00", newPrice: "179.00" },
        { variantTitle: "A5 / Blank", oldPrice: "199.00", newPrice: "184.00" },
      ],
    });

    expect(subject).toContain("Marketos Buzz");
    expect(subject).toContain("Classic Notebook");
    expect(html).toContain("Marketos Buzz");
    expect(html).toContain("Classic Notebook");
    expect(html).toContain("A5 / Ruled");
    expect(html).toContain("199.00");
    expect(html).toContain("179.00");
    expect(html).toContain("A5 / Blank");
    expect(html).toContain("184.00");
  });

  it("includes a % change figure per variant, signed and rounded to one decimal", async () => {
    const { html } = await renderPriceChangeEmail({
      storeName: "Marketos Buzz",
      productTitle: "Classic Notebook",
      currency: "INR",
      variants: [
        { variantTitle: "A5 / Ruled", oldPrice: "199.00", newPrice: "179.00" },
        { variantTitle: "A5 / Blank", oldPrice: "100.00", newPrice: "110.00" },
      ],
    });

    expect(html).toContain("-10.1%");
    expect(html).toContain("+10.0%");
  });

  it("falls back to an em dash instead of NaN/Infinity when oldPrice is zero or non-numeric", async () => {
    const { html } = await renderPriceChangeEmail({
      storeName: "Marketos Buzz",
      productTitle: "Classic Notebook",
      currency: "INR",
      variants: [{ variantTitle: "Free Sample", oldPrice: "0.00", newPrice: "10.00" }],
    });

    expect(html).not.toContain("NaN");
    expect(html).not.toContain("Infinity");
    expect(html).toContain("—");
  });
});
