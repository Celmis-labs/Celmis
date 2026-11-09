import { afterEach, describe, expect, it, vi } from "vitest";

import { oauthClientsApi } from "@/lib/api";

describe("oauthClientsApi.register", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts to the administrator endpoint, not the anonymous registration", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      calls.push({ url: String(url), init });
      return new Response(JSON.stringify({
        client_id: "ec_1", client_secret: "s", name: "acme", redirect_uris: [], allowed_scopes: [],
      }), { status: 201, headers: { "content-type": "application/json" } });
    });
    const out = await oauthClientsApi.register("t", {
      name: "acme", redirect_uris: ["http://localhost:9999/cb"],
      allowed_scopes: ["read:reviews"], public: false,
    });
    expect(out.client_id).toBe("ec_1");
    expect(calls).toHaveLength(1);
    expect(new URL(calls[0].url, "http://x").pathname.endsWith("/oauth/clients")).toBe(true);
    expect(calls[0].url.endsWith("/oauth/register")).toBe(false);
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({
      name: "acme", redirect_uris: ["http://localhost:9999/cb"],
      allowed_scopes: ["read:reviews"], public: false,
    });
  });
});
