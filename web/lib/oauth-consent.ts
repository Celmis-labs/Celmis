/**
 * The consent page's logic, apart from its markup: reading the authorization
 * request out of the address, asking the API what to show, sending the
 * decision, and deciding whether the address the API answers with is one the
 * browser may be sent to.
 *
 * An MCP client (Claude Code, a custom connector) sends the browser to
 * /oauth/consent through the API's /oauth/authorize, which has already checked
 * the client, the redirect URI, PKCE and the scopes. The page checks nothing of
 * that again; it shows what it was told and relays the person's answer.
 */

import { api } from "./api";

/** What the client asked for, exactly as /oauth/authorize forwarded it. */
export interface ConsentRequest {
  client_id: string;
  redirect_uri: string;
  code_challenge: string;
  code_challenge_method: string;
  scope: string;
  state: string;
  resource: string | null;
}

export interface ConsentWorkspace {
  id: string;
  slug: string;
  name: string;
  role: string | null;
}

export interface ConsentScope {
  scope: string;
  granted: boolean;
  /** Why a requested scope is withheld, in the server's words. */
  reason: string | null;
}

export interface ConsentInfo {
  client: {
    client_id: string;
    name: string;
    redirect_host: string;
    registered_by_administrator: boolean;
  };
  workspace: ConsentWorkspace;
  workspaces: ConsentWorkspace[];
  scopes: ConsentScope[];
  granted: string[];
  /** Set when this person may not connect an app here: the sentence to show. */
  blocked: string | null;
}

/** The request in the address, or null when it is not one (a person who opened
 * /oauth/consent by hand, or a link cut short). */
export function parseConsentRequest(params: URLSearchParams): ConsentRequest | null {
  const client_id = params.get("client_id") ?? "";
  const redirect_uri = params.get("redirect_uri") ?? "";
  const code_challenge = params.get("code_challenge") ?? "";
  if (!client_id || !redirect_uri || !code_challenge) return null;
  return {
    client_id,
    redirect_uri,
    code_challenge,
    code_challenge_method: params.get("code_challenge_method") || "S256",
    scope: params.get("scope") ?? "",
    state: params.get("state") ?? "",
    resource: params.get("resource") || null,
  };
}

function infoQuery(req: ConsentRequest, workspace?: string | null): string {
  const qs = new URLSearchParams({
    client_id: req.client_id,
    redirect_uri: req.redirect_uri,
    code_challenge: req.code_challenge,
    code_challenge_method: req.code_challenge_method,
    scope: req.scope,
  });
  if (req.resource) qs.set("resource", req.resource);
  if (workspace) qs.set("workspace", workspace);
  return qs.toString();
}

export function fetchConsentInfo(
  token: string, req: ConsentRequest, workspace?: string | null,
): Promise<ConsentInfo> {
  return api<ConsentInfo>(`/oauth/consent-info?${infoQuery(req, workspace)}`, { token });
}

/** Send Allow or Deny. Resolves to the address the API says to go to. */
export async function submitConsent(
  token: string, req: ConsentRequest, decision: "allow" | "deny", workspace?: string | null,
): Promise<string> {
  const out = await api<{ redirect_to: string }>("/oauth/authorize/consent", {
    token,
    method: "POST",
    json: { ...req, workspace: workspace ?? null, decision },
  });
  return out.redirect_to;
}

/** Whether `target` is where the registered redirect URI points.
 *
 * The API builds the answer from the URI the client registered, so this is
 * belt and braces: only http(s), and the same origin and path as the request
 * named. A response that says otherwise is not followed. (Loopback clients pick
 * their port at start-up, and the API echoes the port the request used.) */
export function isSafeRedirect(target: string, requested: string): boolean {
  try {
    // Browsers read `\` as `/` and an `@` as the start of the host, so a target
    // like `http://evil.example\@localhost/cb` names a different site here than
    // in the API's parser. Nothing legitimate has either before the path, and no
    // control character, fragment or encoded backslash belongs anywhere.
    const rest = target.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
    const authority = rest.split(/[/?#]/, 1)[0] ?? "";
    if (authority.includes("\\") || authority.includes("@")) return false;
    if (/[\s\u0000-\u001f\u007f\\]/.test(target) || target.includes("#")) return false;
    if (/%5c/i.test(target.split("?", 1)[0])) return false;   // the query may carry any `state`
    const a = new URL(target);
    const b = new URL(requested);
    if (a.protocol !== "https:" && a.protocol !== "http:") return false;
    return a.protocol === b.protocol && a.host === b.host && a.pathname === b.pathname;
  } catch {
    return false;
  }
}

/** The sign-in page's address for a person who is not signed in yet, carrying
 * the whole consent request so they land back on it. */
export function loginAddressFor(pathname: string, search: string): string {
  return `/login?from=${encodeURIComponent(pathname + search)}`;
}

const KNOWN_SCOPES = new Set([
  "read:graph", "read:groups", "read:reviews", "read:code",
  "write:repos", "write:config", "write:reviews",
]);

/** The dictionary key that says a scope in plain words; unknown scopes get a
 * generic line, never a raw machine name as the only explanation. */
export function scopeKey(scope: string): string {
  return KNOWN_SCOPES.has(scope) ? `oauth.scope.${scope}` : "oauth.scope.other";
}

export function isWriteScope(scope: string): boolean {
  return scope.startsWith("write:");
}
