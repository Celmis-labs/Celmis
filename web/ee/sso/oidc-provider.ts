// Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
// see LICENSING.md and ee/README.md in the repository root.

/**
 * Company SSO (Keycloak or any OpenID provider) for NextAuth.
 *
 * Registered only when fully configured — AUTH_OIDC_ISSUER, _CLIENT_ID and
 * _CLIENT_SECRET — for the same reason Google is: a provider that cannot work
 * must not be offered. The Keycloak provider is a plain OIDC provider with
 * discovery from `issuer`, so it serves both cases; the id is "oidc", making
 * the redirect URI /api/auth/callback/oidc.
 *
 * Registration does not need a licence, and neither does the exchange for the
 * first five people (LICENSE_EE section 2). The id_token is posted to the
 * API's /api/auth/oidc (src/ee/sso), which is always mounted and refuses a
 * sixth person without a licence that grants "sso" (a seat-limit redirect to
 * /login, see auth.ts). A build whose API has no src/ee does not mount it at
 * all: that request 404s, auth.ts sends the person back to /login, and the
 * login page never showed the button anyway — it asks /api/capabilities first
 * (app/login/page.tsx).
 */
import type { Provider } from "next-auth/providers";
import Keycloak from "next-auth/providers/keycloak";

import { oidcConfigured, oidcProviderName } from "@/lib/auth-options";

/** NextAuth provider id. */
export const OIDC_PROVIDER_ID = "oidc";

/** Where auth.ts exchanges the provider's id_token for a Celmis session. */
export const OIDC_EXCHANGE_PATH = "/api/auth/oidc";

export function oidcProviders(): Provider[] {
  if (!oidcConfigured()) return [];
  return [
    Keycloak({
      id: OIDC_PROVIDER_ID,
      name: oidcProviderName(),
      issuer: process.env.AUTH_OIDC_ISSUER,
      clientId: process.env.AUTH_OIDC_CLIENT_ID,
      clientSecret: process.env.AUTH_OIDC_CLIENT_SECRET,
      authorization: { params: { scope: "openid email profile" } },
    }),
  ];
}
