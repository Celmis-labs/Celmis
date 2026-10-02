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
