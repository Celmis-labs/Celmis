import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildEndSessionUrl,
  discoverEndSessionEndpoint,
  postLogoutRedirectUri,
  resetDiscoveryCache,
} from "./federated-logout";

const ISSUER = "https://idp.example.com/realms/main";

function discovery(doc: unknown, ok = true) {
  return vi.fn(async () => ({ ok, json: async () => doc }) as unknown as Response) as unknown as typeof fetch;
}

beforeEach(() => resetDiscoveryCache());

describe("buildEndSessionUrl", () => {
  it("sends id_token_hint, client_id and post_logout_redirect_uri to the advertised endpoint", async () => {
    const fetchImpl = discovery({ end_session_endpoint: `${ISSUER}/protocol/openid-connect/logout` });
    const url = await buildEndSessionUrl({
      issuer: ISSUER,
      clientId: "celmis",
      idToken: "id.token.value",
      postLogoutRedirectUri: "https://celmis.example.com/login",
      fetchImpl,
    });
    const u = new URL(url!);
    expect(u.origin + u.pathname).toBe(`${ISSUER}/protocol/openid-connect/logout`);
    expect(u.searchParams.get("id_token_hint")).toBe("id.token.value");
    expect(u.searchParams.get("client_id")).toBe("celmis");
    expect(u.searchParams.get("post_logout_redirect_uri")).toBe("https://celmis.example.com/login");
    expect(fetchImpl).toHaveBeenCalledWith(`${ISSUER}/.well-known/openid-configuration`, expect.anything());
  });

  it("is skipped when the provider publishes no end_session_endpoint", async () => {
    const url = await buildEndSessionUrl({
      issuer: ISSUER, clientId: "c", idToken: "t", postLogoutRedirectUri: "x",
      fetchImpl: discovery({ issuer: ISSUER }),
    });
    expect(url).toBeNull();
  });

  it("is skipped for a password or master login (no id_token)", async () => {
    const fetchImpl = discovery({ end_session_endpoint: `${ISSUER}/logout` });
    expect(await buildEndSessionUrl({ issuer: ISSUER, clientId: "c", idToken: "", postLogoutRedirectUri: "x", fetchImpl })).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("is skipped, not thrown, when discovery fails", async () => {
    const boom = vi.fn(async () => { throw new Error("down"); }) as unknown as typeof fetch;
    expect(await buildEndSessionUrl({ issuer: ISSUER, clientId: "c", idToken: "t", postLogoutRedirectUri: "x", fetchImpl: boom })).toBeNull();
    expect(await buildEndSessionUrl({ issuer: ISSUER, clientId: "c", idToken: "t", postLogoutRedirectUri: "x", fetchImpl: discovery({}, false) })).toBeNull();
  });

  it("refuses an end-session endpoint that is not http(s)", async () => {
    const f = discovery({ end_session_endpoint: "javascript:alert(1)" });
    expect(await discoverEndSessionEndpoint(ISSUER, f)).toBeNull();
  });
});

describe("discoverEndSessionEndpoint cache", () => {
  it("asks the provider once within ten minutes", async () => {
    const f = discovery({ end_session_endpoint: `${ISSUER}/logout` });
    let t = 1_000;
    await discoverEndSessionEndpoint(ISSUER, f, () => t);
    t += 9 * 60 * 1000;
    await discoverEndSessionEndpoint(ISSUER, f, () => t);
    expect(f).toHaveBeenCalledTimes(1);
    t += 2 * 60 * 1000;
    await discoverEndSessionEndpoint(ISSUER, f, () => t);
    expect(f).toHaveBeenCalledTimes(2);
  });
});

describe("postLogoutRedirectUri", () => {
  const headers = (h: Record<string, string>) => ({ get: (n: string) => h[n.toLowerCase()] ?? null });

  it("prefers an explicit setting, then NEXTAUTH_URL, then the forwarded host", () => {
    expect(postLogoutRedirectUri({ AUTH_OIDC_POST_LOGOUT_REDIRECT_URI: "https://a.example.com/bye" }, headers({}), "http://web:3000"))
      .toBe("https://a.example.com/bye");
    expect(postLogoutRedirectUri({ NEXTAUTH_URL: "https://celmis.example.com/" }, headers({}), "http://web:3000"))
      .toBe("https://celmis.example.com/login");
    expect(postLogoutRedirectUri({}, headers({ "x-forwarded-host": "celmis.example.com", "x-forwarded-proto": "https" }), "http://web:3000"))
      .toBe("https://celmis.example.com/login");
    expect(postLogoutRedirectUri({}, headers({}), "http://web:3000")).toBe("http://web:3000/login");
  });
});
