import { describe, expect, it } from "vitest";

import {
  isSafeRedirect, isWriteScope, loginAddressFor, parseConsentRequest, scopeKey,
} from "./oauth-consent";

const q = (s: string) => new URLSearchParams(s);

describe("parseConsentRequest", () => {
  it("reads what /oauth/authorize forwarded", () => {
    const r = parseConsentRequest(q(
      "client_id=ec_1&redirect_uri=http%3A%2F%2Flocalhost%3A5%2Fcb&code_challenge=abc&scope=read%3Agraph&state=s&resource=https%3A%2F%2Fx.example%2Fbackend%2Fmcp",
    ));
    expect(r).toEqual({
      client_id: "ec_1", redirect_uri: "http://localhost:5/cb", code_challenge: "abc",
      code_challenge_method: "S256", scope: "read:graph", state: "s",
      resource: "https://x.example/backend/mcp",
    });
  });
  it("is null when the address is not an authorization request", () => {
    expect(parseConsentRequest(q(""))).toBeNull();
    expect(parseConsentRequest(q("client_id=a&redirect_uri=b"))).toBeNull();
  });
});

describe("isSafeRedirect", () => {
  const want = "http://localhost:5000/callback";
  it("accepts the requested address with the answer in its query", () => {
    expect(isSafeRedirect("http://localhost:5000/callback?code=1&state=2", want)).toBe(true);
  });
  it("refuses another host, port, path, scheme or a script URL", () => {
    for (const bad of [
      "https://evil.example/callback", "http://localhost:5001/callback",
      "http://localhost:5000/other", "https://localhost:5000/callback",
      "javascript:alert(1)", "data:text/html,x", "not a url", "",
    ]) expect(isSafeRedirect(bad, want)).toBe(false);
  });
  it("keeps an encoded backslash in the query, where a state may carry one", () => {
    expect(isSafeRedirect("http://localhost:5000/callback?state=a%5Cb&code=1", want)).toBe(true);
  });
  it("refuses the forms a browser and a server read as different hosts", () => {
    for (const bad of [
      "http://evil.example\\@localhost:5000/callback",
      "http://localhost:5000\\@evil.example/callback",
      "http://user:pw@localhost:5000/callback",
      "http://evil.example@localhost:5000/callback",
      "http://localhost:5000/callback#frag",
      "http://localhost:5000/callback?code=1\tx",
      "http://localhost:5000/callback?code=1\nx",
      "http://localhost:5000/call%5cback",
    ]) expect(isSafeRedirect(bad, want), bad).toBe(false);
  });
});

describe("loginAddressFor", () => {
  it("keeps the whole consent request through sign-in", () => {
    const url = loginAddressFor("/oauth/consent", "?client_id=a&state=b%26c");
    expect(url.startsWith("/login?from=")).toBe(true);
    expect(decodeURIComponent(url.slice("/login?from=".length))).toBe("/oauth/consent?client_id=a&state=b%26c");
  });
});

describe("scopes", () => {
  it("names known scopes and falls back for unknown ones", () => {
    expect(scopeKey("read:code")).toBe("oauth.scope.read:code");
    expect(scopeKey("whatever")).toBe("oauth.scope.other");
  });
  it("tells writes from reads", () => {
    expect(isWriteScope("write:repos")).toBe(true);
    expect(isWriteScope("read:graph")).toBe(false);
  });
});
