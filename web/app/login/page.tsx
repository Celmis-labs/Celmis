/**
 * /login — server wrapper so the page knows which sign-in methods this
 * install allows. AUTH_PASSWORD_LOGIN is a runtime env var on the server; a
 * client component could only see it if it were baked into the bundle at
 * build time, which a published image cannot do.
 *
 * Company SSO (enterprise, web/ee/sso) is offered only when BOTH halves are
 * there: the web side is configured for OIDC (AUTH_OIDC_*), and the API
 * reports the `sso` capability — i.e. its licence granted "sso" and it
 * mounted /api/auth/oidc. Either alone is a button that dead-ends.
 *
 * This is the one consumer of /api/capabilities that hides on an UNKNOWN
 * answer, deliberately, and it does not contradict the capabilities contract
 * ("hide only on an explicit false"). That rule protects working pages from a
 * detection failure. Here nothing working is at stake: an unreachable API
 * cannot complete an SSO sign-in either, and the password form — the
 * break-glass path — is not affected by this check at all.
 */
import { LoginForm } from "./login-form";
import { API_BASE } from "@/lib/api";
import { oidcConfigured, oidcProviderName, passwordLoginEnabled } from "@/lib/auth-options";

// Read the env per request, not once at build.
export const dynamic = "force-dynamic";

/** Does the API say `sso` is mounted? False on any failure (see above). */
async function apiOffersSso(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/api/capabilities`, {
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return false;
    const body = (await res.json()) as { features?: Record<string, { available?: boolean }> };
    return body.features?.sso?.available === true;
  } catch {
    return false;
  }
}

export default async function LoginPage() {
  const ssoName = oidcConfigured() && (await apiOffersSso()) ? oidcProviderName() : null;
  return <LoginForm passwordLogin={passwordLoginEnabled()} ssoName={ssoName} />;
}
