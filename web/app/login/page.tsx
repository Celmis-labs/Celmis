/**
 * /login — server wrapper so the page knows which sign-in methods this
 * install allows. AUTH_PASSWORD_LOGIN is a runtime env var on the server; a
 * client component could only see it if it were baked into the bundle at
 * build time, which a published image cannot do.
 *
 * Company SSO (enterprise, web/ee/sso) is offered only when BOTH halves are
 * there: the web side is configured for OIDC (AUTH_OIDC_*), and the API
 * reports the `sso` capability (`apiOffersSso`, web/lib/sso-offer.ts — shared
 * with the invite landing page, which offers the same buttons).
 */
import { LoginForm } from "./login-form";
import { oidcConfigured, oidcProviderName, passwordLoginEnabled } from "@/lib/auth-options";
import { apiOffersSso } from "@/lib/sso-offer";

// Read the env per request, not once at build.
export const dynamic = "force-dynamic";

export default async function LoginPage() {
  const ssoName = oidcConfigured() && (await apiOffersSso()) ? oidcProviderName() : null;
  return <LoginForm passwordLogin={passwordLoginEnabled()} ssoName={ssoName} />;
}
