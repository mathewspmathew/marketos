/**
 * Integration regression test for Sentry issue PYTHON-7A —
 * "TypeError: Invalid URL" escaping authenticate.admin() as a 500 on
 * GET /app when the request carries a valid `shop` param plus a
 * base64-valid `host` param that decodes to a non-URL ("hello world").
 *
 * The hardened `authenticate` export in shopify.server.js must intercept
 * that exact shape and throw the app-bridge bounce Response instead, and
 * must delegate every other request shape to the real SDK untouched.
 */
import { describe, it, expect, vi } from "vitest";

// Minimal in-memory session storage: shopify.server.js only needs the
// storage interface; no test here exercises real session persistence.
const memorySessions = new Map();
vi.mock("./db.server", () => ({
  default: {
    shopifyUser: { upsert: vi.fn() },
    shopSettings: { upsert: vi.fn() },
    session: {
      create: async ({ data }) => {
        memorySessions.set(data.id, data);
        return data;
      },
      findUnique: async ({ where }) => memorySessions.get(where.id),
      delete: async ({ where }) => memorySessions.delete(where.id),
      deleteMany: async () => undefined,
      count: async () => memorySessions.size,
    },
  },
}));

import shopifyDefault, { authenticate } from "./shopify.server";

// b64("hello world") — base64-valid, decodes to a non-URL. This is the
// exact param pair captured in the Sentry event (host=aGVsbG8gd29ybGQ=).
const CRASH_HOST = btoa("hello world");
const GOOD_HOST = btoa("acme.myshopify.com");

function appRequest(search) {
  return new Request(`http://localhost:3000/app?${search}`);
}

async function outcome(request) {
  try {
    await authenticate.admin(request);
    return { kind: "authenticated" };
  } catch (err) {
    if (err instanceof Response) {
      return { kind: "response", status: err.status };
    }
    return { kind: "throw", name: err.constructor.name, message: err.message };
  }
}

describe("hardened authenticate.admin (Sentry PYTHON-7A regression)", () => {
  it("bounces the crash shape (valid shop + b64 host that is not a URL) instead of throwing TypeError", async () => {
    const result = await outcome(
      appRequest(`shop=acme.myshopify.com&host=${CRASH_HOST}`),
    );
    expect(result).toMatchObject({ kind: "response" });
    expect(result.status).toBe(200); // app-bridge bounce page, not a 500
  });

  it("bounces the crash shape with embedded=1 too", async () => {
    const result = await outcome(
      appRequest(`shop=acme.myshopify.com&host=${CRASH_HOST}&embedded=1`),
    );
    expect(result).toMatchObject({ kind: "response", status: 200 });
  });

  it("still delegates a fully valid embedded request to the SDK (token check runs)", async () => {
    // Valid shop + host reaches the SDK; without a session token it gets the
    // SDK's own 302 bounce — proving the wrapper did not swallow the request.
    const result = await outcome(
      appRequest(`shop=acme.myshopify.com&host=${GOOD_HOST}&embedded=1`),
    );
    expect(result).toMatchObject({ kind: "response", status: 302 });
  });

  it("still delegates requests with no shop param to the SDK (SDK app-bridge page)", async () => {
    const result = await outcome(appRequest(""));
    expect(result).toMatchObject({ kind: "response", status: 200 });
  });

  it("still delegates bot requests to the SDK (410 Gone)", async () => {
    const request = new Request("http://localhost:3000/app", {
      headers: { "User-Agent": "python-requests/2.0" },
    });
    const result = await outcome(request);
    expect(result).toMatchObject({ kind: "response", status: 410 });
  });

  it("does not alter non-admin auth methods (webhook auth still the SDK's)", () => {
    expect(authenticate.webhook).toBe(shopifyDefault.authenticate.webhook);
  });
});
