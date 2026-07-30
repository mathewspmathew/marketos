import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../lib/resendClient.server", () => ({ sendEmail: vi.fn() }));
vi.mock("../db.server", () => ({
  default: { shopSettings: { findUnique: vi.fn() } },
}));

import { action } from "./internal.notify-price-change";
import { sendEmail } from "../lib/resendClient.server";
import db from "../db.server";

function makeRequest(body, headers = {}) {
  return new Request("http://localhost/internal.notify-price-change", {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

describe("internal.notify-price-change action", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    process.env.INTERNAL_API_TOKEN = "test-token";
  });

  it("rejects requests without a valid X-Internal-Token", async () => {
    const res = await action({ request: makeRequest({}, { "x-internal-token": "wrong" }) });
    expect(res.status).toBe(403);
  });

  it("rejects requests missing required fields", async () => {
    const res = await action({
      request: makeRequest({ shopDomain: "shop.myshopify.com" }, { "x-internal-token": "test-token" }),
    });
    expect(res.status).toBe(400);
  });

  it("sends the email using the shop's stored notifyEmail and returns ok:true on success", async () => {
    db.shopSettings.findUnique.mockResolvedValue({ notifyEmail: "merchant@example.com" });
    sendEmail.mockResolvedValue({ ok: true });

    const res = await action({
      request: makeRequest(
        {
          shopDomain: "marketos-buzz.myshopify.com",
          productTitle: "Classic Notebook",
          currency: "INR",
          variants: [{ variantTitle: "A5 / Ruled", oldPrice: "199.00", newPrice: "179.00" }],
        },
        { "x-internal-token": "test-token" },
      ),
    });
    const data = await res.json();

    expect(data.ok).toBe(true);
    expect(sendEmail).toHaveBeenCalledWith(
      expect.objectContaining({ to: "merchant@example.com" }),
    );
  });

  it("returns ok:false without throwing when the shop has no notifyEmail set", async () => {
    db.shopSettings.findUnique.mockResolvedValue({ notifyEmail: null });

    const res = await action({
      request: makeRequest(
        {
          shopDomain: "marketos-buzz.myshopify.com",
          productTitle: "Classic Notebook",
          currency: "INR",
          variants: [{ variantTitle: "A5 / Ruled", oldPrice: "199.00", newPrice: "179.00" }],
        },
        { "x-internal-token": "test-token" },
      ),
    });
    const data = await res.json();

    expect(data.ok).toBe(false);
    expect(sendEmail).not.toHaveBeenCalled();
  });
});
