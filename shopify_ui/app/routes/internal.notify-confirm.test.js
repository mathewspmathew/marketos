import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../lib/resendClient.server", () => ({ sendEmail: vi.fn() }));

import { action } from "./internal.notify-confirm";
import { sendEmail } from "../lib/resendClient.server";

function makeRequest(body, headers = {}) {
  return new Request("http://localhost/internal.notify-confirm", {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

describe("internal.notify-confirm action", () => {
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

  it("sends the confirmation email and returns ok:true on success", async () => {
    sendEmail.mockResolvedValue({ ok: true });

    const res = await action({
      request: makeRequest(
        { shopDomain: "marketos-buzz.myshopify.com", email: "merchant@example.com" },
        { "x-internal-token": "test-token" },
      ),
    });
    const data = await res.json();

    expect(data.ok).toBe(true);
    expect(sendEmail).toHaveBeenCalledWith(
      expect.objectContaining({ to: "merchant@example.com" }),
    );
  });

  it("returns ok:false with the error when sendEmail fails", async () => {
    sendEmail.mockResolvedValue({ ok: false, error: "bad address" });

    const res = await action({
      request: makeRequest(
        { shopDomain: "marketos-buzz.myshopify.com", email: "not-an-email" },
        { "x-internal-token": "test-token" },
      ),
    });
    const data = await res.json();

    expect(data.ok).toBe(false);
    expect(data.error).toBe("bad address");
  });
});
