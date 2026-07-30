import { describe, it, expect } from "vitest";
import { renderConfirmationEmail } from "./notificationsEnabledConfirmation.server";

describe("renderConfirmationEmail", () => {
  it("includes store name and the confirmed address", async () => {
    const { subject, html } = await renderConfirmationEmail({
      storeName: "Marketos Buzz",
      email: "merchant@example.com",
    });

    expect(subject).toContain("Marketos Buzz");
    expect(html).toContain("Marketos Buzz");
    expect(html).toContain("merchant@example.com");
  });
});
