/**
 * Server-side switches for the sign-in methods, read from the environment.
 *
 * Shared by `auth.ts` (which providers to register) and the login page
 * (which forms to show). Read at request time, never inlined at build time:
 * the Docker image is built once and configured by env at runtime.
 */

/** AUTH_PASSWORD_LOGIN=false hides email+password sign-in and signup.
 *  The master-key login keeps working (break-glass for when the IdP is down). */
export function passwordLoginEnabled(): boolean {
  const raw = (process.env.AUTH_PASSWORD_LOGIN ?? "").trim().toLowerCase();
  return !["0", "false", "no", "off"].includes(raw);
}

/** Company SSO (Keycloak / OIDC) is offered only when fully configured. */
export function oidcConfigured(): boolean {
  return Boolean(
    process.env.AUTH_OIDC_ISSUER &&
      process.env.AUTH_OIDC_CLIENT_ID &&
      process.env.AUTH_OIDC_CLIENT_SECRET,
  );
}

/** Button label: "Sign in with <name>". */
export function oidcProviderName(): string {
  return (process.env.AUTH_OIDC_NAME ?? "").trim() || "SSO";
}

/** The web session's life in seconds: CELMIS_WEB_SESSION_HOURS, 24 h at most.
 *  It slides: the cookie is re-issued while the person keeps using the app. */
export function sessionMaxAgeSeconds(): number {
  const hours = Number.parseFloat(process.env.CELMIS_WEB_SESSION_HOURS ?? "");
  const h = Number.isFinite(hours) && hours > 0 ? Math.min(hours, 24) : 24;
  return Math.round(h * 3600);
}

/** Life of the API access token in seconds (the API clamps it to 60..3600). */
export function accessTokenTtlSeconds(): number {
  const raw = Number.parseInt(process.env.CELMIS_ACCESS_TOKEN_TTL_SECONDS ?? "", 10);
  const v = Number.isFinite(raw) ? raw : 3600;
  return Math.max(60, Math.min(3600, v));
}

/** How early before expiry the access token is renewed, in ms: five minutes,
 *  or half its life when it is shorter than ten. */
export function renewBeforeMs(): number {
  return Math.min(5 * 60 * 1000, (accessTokenTtlSeconds() * 1000) / 2);
}
