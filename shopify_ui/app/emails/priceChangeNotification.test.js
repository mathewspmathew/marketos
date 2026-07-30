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
});
