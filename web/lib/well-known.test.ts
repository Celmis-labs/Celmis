import { describe, expect, it } from "vitest";

import { API_PREFIX, resolveWellKnown } from "./well-known";

const ok = (segments: string[]) => {
  const r = resolveWellKnown(segments);
  if (!r.ok) throw new Error(`expected a target, got ${r.error}: ${r.description}`);
  return r.upstreamPath;
};

describe("resolveWellKnown", () => {
  it("passes the two OAuth documents through at the bare path", () => {
    expect(ok(["oauth-protected-resource"])).toBe("/.well-known/oauth-protected-resource");
    expect(ok(["oauth-authorization-server"])).toBe("/.well-known/oauth-authorization-server");
  });

  it("takes the API prefix off a path-aware document name", () => {
    expect(ok(["oauth-protected-resource", "backend", "mcp", "dev"]))
      .toBe("/.well-known/oauth-protected-resource/mcp/dev");
    expect(ok(["oauth-protected-resource", "backend", "mcp"]))
      .toBe("/.well-known/oauth-protected-resource/mcp");
    expect(ok(["oauth-authorization-server", "backend"]))
      .toBe("/.well-known/oauth-authorization-server");
  });

  it("answers a resource that is not under the API prefix with a pointer, not with a document", () => {
    const r = resolveWellKnown(["oauth-protected-resource", "mcp", "dev"]);
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.error).toBe("not_found");
      expect(r.description).toContain(`/.well-known/oauth-protected-resource${API_PREFIX}/`);
    }
  });

  it("serves nothing else under /.well-known", () => {
    for (const name of ["openid-configuration", "security.txt", "assetlinks.json", "change-password", "x"]) {
      const r = resolveWellKnown([name]);
      expect(r.ok, name).toBe(false);
      if (!r.ok) expect(r.error).toBe("not_found");
    }
  });

  it("refuses an empty path and a path that is too deep", () => {
    expect(resolveWellKnown([]).ok).toBe(false);
    expect(resolveWellKnown(["oauth-protected-resource", "backend", "a", "b", "c", "d", "e"]).ok).toBe(false);
  });

  it("never lets a segment steer the upstream path somewhere else", () => {
    const bad = ["..", ".", "a/b", "a%2fb", "a b", "a?x=1", "a#f", "a\\b", "", "x".repeat(65), "a\nb", "%2e%2e"];
    for (const seg of bad) {
      expect(resolveWellKnown(["oauth-protected-resource", "backend", seg]).ok, JSON.stringify(seg)).toBe(false);
    }
    expect(resolveWellKnown(["oauth-protected-resource", "backend", "mcp", ".."]).ok).toBe(false);
  });

  it("always answers a refusal as data a JSON client can read", () => {
    for (const segs of [["x"], ["oauth-protected-resource", "mcp"], []]) {
      const r = resolveWellKnown(segs);
      expect(r.ok).toBe(false);
      if (!r.ok) {
        expect(typeof r.error).toBe("string");
        expect(r.description.length).toBeGreaterThan(0);
      }
    }
  });
});
