/**
 * Sign-out at the identity provider too (OpenID Connect RP-Initiated Logout).
 *
 * Ending only the Celmis session leaves the provider's own session alive, so
 * the next "Sign in with SSO" completes silently and a shared browser stays
 * signed in to everything else the provider fronts. The provider publishes
 * where to send the browser in its discovery document
 * (`end_session_endpoint`); nothing here is specific to one provider.
 *
 * The result is `null` — "just end the local session" — whenever there is
 * nothing to do: the person did not sign in through the provider (password or
 * master login, Google), the provider has no end-session endpoint, or the
 * discovery document cannot be read. Sign-out must never fail because of this.
 */

export type EndSessionInput = {
  issuer: string;
  clientId: string;
  idToken: string;
  postLogoutRedirectUri: string;
  /** Injected by tests. */
  fetchImpl?: typeof fetch;
};

const DISCOVERY_TTL_MS = 10 * 60 * 1000;
const cache = new Map<string, { at: number; endpoint: string | null }>();

/** `end_session_endpoint` from OIDC discovery, cached for ten minutes. */
export async function discoverEndSessionEndpoint(
  issuer: string,
  fetchImpl: typeof fetch = fetch,
  now: () => number = Date.now,
): Promise<string | null> {
  const hit = cache.get(issuer);
  if (hit && now() - hit.at < DISCOVERY_TTL_MS) return hit.endpoint;

  let endpoint: string | null = null;
  try {
    const url = `${issuer.replace(/\/+$/, "")}/.well-known/openid-configuration`;
    const resp = await fetchImpl(url, { signal: AbortSignal.timeout(4000), cache: "no-store" });
    if (resp.ok) {
      const doc = (await resp.json()) as { end_session_endpoint?: unknown };
      const raw = doc.end_session_endpoint;
      if (typeof raw === "string") {
        const u = new URL(raw);
        if (u.protocol === "https:" || u.protocol === "http:") endpoint = u.toString();
      }
    }
  } catch {
    endpoint = null;
  }
  cache.set(issuer, { at: now(), endpoint });
  return endpoint;
}

/** Where to send the browser to end the provider's session, or null. */
export async function buildEndSessionUrl(input: EndSessionInput): Promise<string | null> {
  if (!input.issuer || !input.idToken) return null;
  const endpoint = await discoverEndSessionEndpoint(input.issuer, input.fetchImpl);
  if (!endpoint) return null;
  const url = new URL(endpoint);
  url.searchParams.set("id_token_hint", input.idToken);
  url.searchParams.set("client_id", input.clientId);
  url.searchParams.set("post_logout_redirect_uri", input.postLogoutRedirectUri);
  return url.toString();
}

/** The address the provider sends the browser back to: `<origin>/login`.
 *  AUTH_OIDC_POST_LOGOUT_REDIRECT_URI wins; else NEXTAUTH_URL / AUTH_URL;
 *  else what the proxy forwarded. The result must be registered at the provider
 *  as a valid post-logout redirect URI. */
export function postLogoutRedirectUri(
  env: Record<string, string | undefined>,
  headers: { get(name: string): string | null },
  requestOrigin: string,
): string {
  const explicit = (env.AUTH_OIDC_POST_LOGOUT_REDIRECT_URI ?? "").trim();
  if (explicit) return explicit;
  const configured = (env.NEXTAUTH_URL ?? env.AUTH_URL ?? "").trim();
  if (configured) {
    try {
      return `${new URL(configured).origin}/login`;
    } catch {
      // fall through to the request
    }
  }
  const host = headers.get("x-forwarded-host")?.split(",")[0].trim();
  const proto = headers.get("x-forwarded-proto")?.split(",")[0].trim();
  if (host && (proto === "http" || proto === "https")) return `${proto}://${host}/login`;
  return `${requestOrigin}/login`;
}

/** Test hook. */
export function resetDiscoveryCache(): void {
  cache.clear();
}
