/**
 * What the web host answers under `/.well-known/`.
 *
 * Why this host answers at all: an MCP client that does not (or cannot) follow
 * the metadata link of a 401 looks for the sign-in documents at the HOST ROOT
 * (RFC 9728 / RFC 8414), and the host root belongs to this app. Before this
 * existed the sign-in guard answered those requests with a redirect to /login
 * and the client read the login page as JSON. Now the two OAuth documents are
 * the API's own, fetched through, and everything else is a JSON 404: this path
 * is read by programs, never by a person, so it never answers with HTML and the
 * sign-in guard never redirects it to /login. (One redirect is not ours: Next
 * itself sends `/.well-known/` to `/.well-known` with a 308 before any handler
 * runs; the JSON 404 is what that lands on.)
 *
 * The API lives under `/backend` here (the reverse proxy takes the prefix off
 * before the API sees the request). A path-aware well-known URL therefore
 * carries it: `/.well-known/oauth-protected-resource/backend/mcp/dev` is the
 * document of the API's `/mcp/dev`.
 */

/** Where the API sits behind the bundled reverse proxy. */
export const API_PREFIX = "/backend";

/** The two documents the API publishes; nothing else is proxied. */
export const OAUTH_DOCUMENTS = ["oauth-protected-resource", "oauth-authorization-server"] as const;

export type WellKnownTarget =
  | { ok: true; upstreamPath: string }
  | { ok: false; error: string; description: string };

const SEGMENT = /^[A-Za-z0-9._~-]{1,64}$/;

/**
 * Where a `/.well-known/<segments>` request goes.
 *
 * `segments` is what follows `/.well-known/`. The API path comes back without
 * its prefix, because the API itself is mounted at the root of its own server.
 */
export function resolveWellKnown(segments: readonly string[]): WellKnownTarget {
  const refuse = (description: string): WellKnownTarget => ({
    ok: false, error: "not_found", description,
  });
  if (segments.length === 0 || segments.length > 6 || !segments.every((s) => SEGMENT.test(s) && s !== "." && s !== "..")) {
    return refuse("Not found.");
  }
  const [name, ...rest] = segments;
  if (!(OAUTH_DOCUMENTS as readonly string[]).includes(name)) {
    return refuse(
      `Nothing is published at /.well-known/${name} on this host. ` +
      "Celmis publishes only the OAuth discovery documents here.",
    );
  }
  if (rest.length === 0) return { ok: true, upstreamPath: `/.well-known/${name}` };
  if (rest[0] !== API_PREFIX.slice(1)) {
    return refuse(
      `This host has no MCP resource at /${rest.join("/")}. The Celmis API is served under ` +
      `${API_PREFIX}: look for /.well-known/${name}${API_PREFIX}/...`,
    );
  }
  const tail = rest.slice(1);
  return { ok: true, upstreamPath: `/.well-known/${name}${tail.length ? `/${tail.join("/")}` : ""}` };
}
