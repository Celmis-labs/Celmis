import { describe, expect, it } from "vitest";

import {
  buildCsp,
  cspHeader,
  cspMode,
  HSTS,
  isHttps,
  makeNonce,
  originOf,
  STATIC_SECURITY_HEADERS,
} from "./csp";

const directive = (csp: string, name: string) =>
  csp.split("; ").find((d) => d.startsWith(`${name} `)) ?? "";

describe("buildCsp", () => {
  const csp = buildCsp({ nonce: "abc123" });

  it("lets scripts run only by nonce, with strict-dynamic and no eval", () => {
    const script = directive(csp, "script-src");
    expect(script).toContain("'nonce-abc123'");
    expect(script).toContain("'strict-dynamic'");
    expect(script).not.toContain("'unsafe-inline'");
    expect(script).not.toContain("'unsafe-eval'");
  });

  it("allows unsafe-eval only in development", () => {
    expect(directive(buildCsp({ nonce: "n", isDev: true }), "script-src")).toContain("'unsafe-eval'");
    expect(directive(buildCsp({ nonce: "n", isDev: false }), "script-src")).not.toContain("'unsafe-eval'");
  });

  it("forbids framing, plugins and a rewritten base", () => {
    expect(directive(csp, "frame-ancestors")).toBe("frame-ancestors 'none'");
    expect(directive(csp, "object-src")).toBe("object-src 'none'");
    expect(directive(csp, "base-uri")).toBe("base-uri 'self'");
  });

  it("keeps unsafe-inline for STYLES only (style attributes are rendered into the HTML)", () => {
    expect(directive(csp, "style-src")).toContain("'unsafe-inline'");
    expect(csp.match(/'unsafe-inline'/g)).toHaveLength(1);
  });

  it("lets a form post end in a redirect to the OIDC issuer", () => {
    const withIdp = buildCsp({ nonce: "n", oidcIssuer: "https://idp.example.com/realms/main" });
    expect(directive(withIdp, "form-action")).toBe("form-action 'self' https://idp.example.com");
    expect(directive(csp, "form-action")).toBe("form-action 'self'");
  });

  it("adds Google to form-action only when Google sign-in is configured", () => {
    expect(directive(buildCsp({ nonce: "n", googleEnabled: true }), "form-action")).toContain(
      "https://accounts.google.com",
    );
    expect(directive(csp, "form-action")).not.toContain("google");
  });

  it("covers an API on another origin in connect-src, and a relative one by 'self'", () => {
    expect(directive(buildCsp({ nonce: "n", apiBase: "https://api.example.com/v1" }), "connect-src")).toBe(
      "connect-src 'self' https://api.example.com",
    );
    expect(directive(buildCsp({ nonce: "n", apiBase: "/backend" }), "connect-src")).toBe("connect-src 'self'");
  });

  it("ignores an issuer that is not a URL", () => {
    expect(directive(buildCsp({ nonce: "n", oidcIssuer: "not a url" }), "form-action")).toBe("form-action 'self'");
    expect(originOf("javascript:alert(1)")).toBeNull();
  });
});

describe("cspMode / cspHeader", () => {
  it("defaults to enforce and understands the valve values", () => {
    expect(cspMode(undefined)).toBe("enforce");
    expect(cspMode("")).toBe("enforce");
    expect(cspMode("garbage")).toBe("enforce");
    expect(cspMode(" Report-Only ")).toBe("report-only");
    expect(cspMode("OFF")).toBe("off");
  });

  it("names the header for the mode, and sends none when off", () => {
    expect(cspHeader("enforce", "p")).toEqual(["Content-Security-Policy", "p"]);
    expect(cspHeader("report-only", "p")).toEqual(["Content-Security-Policy-Report-Only", "p"]);
    expect(cspHeader("off", "p")).toBeNull();
  });
});

describe("nonce and headers", () => {
  it("is fresh each time and is base64", () => {
    const a = makeNonce();
    const b = makeNonce();
    expect(a).not.toBe(b);
    expect(a).toMatch(/^[A-Za-z0-9+/]{22}==$/);
  });

  it("carries the fixed security headers", () => {
    const h = Object.fromEntries(STATIC_SECURITY_HEADERS);
    expect(h["X-Content-Type-Options"]).toBe("nosniff");
    expect(h["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
    expect(h["X-Frame-Options"]).toBe("DENY");
    for (const denied of ["camera=()", "microphone=()", "geolocation=()"]) {
      expect(h["Permissions-Policy"]).toContain(denied);
    }
    expect(HSTS[0]).toBe("Strict-Transport-Security");
  });

  it("calls a request https from the forwarded proto first, the URL second", () => {
    expect(isHttps("http://x/", "https")).toBe(true);
    expect(isHttps("https://x/", "http")).toBe(false);
    expect(isHttps("https://x/", null)).toBe(true);
    expect(isHttps("http://x/", undefined)).toBe(false);
  });
});
