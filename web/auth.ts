/**
 * NextAuth (Auth.js v5) configuration.
 *
 * Providers:
 *   - Credentials (email + password) → calls FastAPI /api/auth/login
 *   - Google     → calls FastAPI /api/auth/google with the Google id_token
 *   - OIDC (Keycloak or any OpenID provider, id "oidc") → calls FastAPI
 *     /api/auth/oidc with the provider's id_token. Enterprise: the provider
 *     lives in web/ee/sso (LICENSE_EE) and the API endpoint in src/ee/sso,
 *     mounted only under a licence that grants "sso". Registered only when
 *     AUTH_OIDC_ISSUER + AUTH_OIDC_CLIENT_ID + AUTH_OIDC_CLIENT_SECRET are set.
 *
 * The FastAPI backend is the source of truth for the user record. NextAuth
 * stores the backend-issued JWT inside the session so frontend pages can
 * forward it as Bearer to FastAPI.
 */
import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";
import Google from "next-auth/providers/google";

import { api, type TokenResponse, type UserOut } from "@/lib/api";
import { passwordLoginEnabled } from "@/lib/auth-options";
import { OIDC_EXCHANGE_PATH, OIDC_PROVIDER_ID, oidcProviders } from "@/ee/sso/oidc-provider";

/** How often the jwt callback re-reads /api/auth/me (is_admin). */
const ME_RECHECK_MS = 5 * 60 * 1000;

/** Where a sign-in the API refused lands: /login shows auth.sso.failed for
 *  any ?error=. Without it the OAuth callback "succeeded" with no Celmis
 *  token and the proxy bounced the user to /login with no message. */
const SSO_REJECTED_URL = "/login?error=SsoRejected";

/** The backend endpoint that exchanges this provider's id_token, if any. */
function exchangePathFor(provider: string | undefined): string | null {
  if (provider === "google") return "/api/auth/google";
  if (provider === OIDC_PROVIDER_ID) return OIDC_EXCHANGE_PATH;
  return null;
}

/** Exchange a provider id_token at a backend endpoint and load the user. */
async function exchangeIdToken(path: string, idToken: string) {
  const tok = await api<TokenResponse>(path, {
    method: "POST",
    json: { id_token: idToken },
  });
  const me = await api<UserOut>("/api/auth/me", { token: tok.access_token });
  return { tok, me };
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  trustHost: true,
  session: { strategy: "jwt", maxAge: 30 * 24 * 60 * 60 }, // 30 days
  pages: { signIn: "/login" },
  providers: [
    Credentials({
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" },
        mode: { label: "Mode", type: "text" }, // 'login' | 'signup'
        name: { label: "Name", type: "text" },
      },
      authorize: async (raw) => {
        const email = String(raw?.email ?? "").trim();
        const password = String(raw?.password ?? "");
        const mode = String(raw?.mode ?? "login");
        const name = String(raw?.name ?? "");
        if (!email || !password) return null;
        // AUTH_PASSWORD_LOGIN=false: no self-service signup. Login stays
        // routed to the backend, which refuses everything but the master key
        // (the break-glass account for when the IdP is down).
        if (mode === "signup" && !passwordLoginEnabled()) return null;

        const path = mode === "signup" ? "/api/auth/signup" : "/api/auth/login";
        const body =
          mode === "signup"
            ? { email, password, name }
            : { email, password };
        try {
          const tok = await api<TokenResponse>(path, { method: "POST", json: body });
          const me = await api<UserOut>("/api/auth/me", { token: tok.access_token });
          return {
            id: me.id,
            email: me.email,
            name: me.name || me.email,
            celmisToken: tok.access_token,
            celmisExpiresAt: tok.expires_at,
            isAdmin: me.is_admin,
            isSuperadmin: Boolean(me.is_superadmin),
          };
        } catch {
          return null;
        }
      },
    }),
    // Google is registered ONLY when actually configured. Registering it with
    // an empty clientId still renders a "Continue with Google" button that
    // sends Google a request without client_id → "400: invalid_request".
    // A provider that cannot work must not be offered.
    ...(process.env.GOOGLE_CLIENT_ID && process.env.GOOGLE_CLIENT_SECRET
      ? [
          Google({
            clientId: process.env.GOOGLE_CLIENT_ID,
            clientSecret: process.env.GOOGLE_CLIENT_SECRET,
            authorization: { params: { scope: "openid email profile" } },
          }),
        ]
      : []),
    // Company SSO (Keycloak realm or any OIDC issuer) — enterprise, see
    // web/ee/sso/oidc-provider.ts. Same rule as Google: registered only when
    // fully configured, so this is an empty list otherwise.
    ...oidcProviders(),
  ],
  callbacks: {
    // Google / OIDC: exchange the id_token for our JWT HERE, not in `jwt`.
    // `signIn` can refuse with a redirect, so an API rejection (no sso
    // licence, domain not allowed, issuer/audience mismatch, master address)
    // reaches /login as ?error= and the form says so. Without an adapter
    // Auth.js hands this same `user` object to `jwt` below, which stores it
    // exactly like a Credentials sign-in.
    signIn: async ({ user, account }) => {
      const exchangePath = exchangePathFor(account?.provider);
      if (!exchangePath) return true;
      if (!account?.id_token) return SSO_REJECTED_URL;
      try {
        const { tok, me } = await exchangeIdToken(exchangePath, account.id_token);
        Object.assign(user, {
          id: me.id,
          email: me.email,
          name: me.name || me.email,
          celmisToken: tok.access_token,
          celmisExpiresAt: tok.expires_at,
          isAdmin: me.is_admin,
          isSuperadmin: Boolean(me.is_superadmin),
        });
        return true;
      } catch {
        return SSO_REJECTED_URL;
      }
    },
    jwt: async ({ token, user }) => {
      // Initial sign-in: Credentials (authorize()) or Google/OIDC (signIn above).
      if (user && (user as { celmisToken?: string }).celmisToken) {
        const u = user as { id: string; email: string; name: string; celmisToken: string; celmisExpiresAt: string; isAdmin: boolean; isSuperadmin?: boolean };
        token.celmisToken = u.celmisToken;
        token.celmisExpiresAt = u.celmisExpiresAt;
        token.isAdmin = u.isAdmin;
        token.isSuperadmin = Boolean(u.isSuperadmin);
        token.userId = u.id;
        token.email = u.email;
        token.name = u.name;
        token.meCheckedAt = Date.now();
        return token;
      }

      // Stage 21 — silent session refresh. When the backend token is
      // within 24h of expiry, exchange it for a fresh one via
      // /api/auth/refresh (requires the CURRENT token to still be
      // valid). On failure we keep the old token — the user gets logged
      // out naturally at expiry instead of abruptly now.
      const expiresAt = token.celmisExpiresAt as string | undefined;
      if (token.celmisToken && expiresAt) {
        const msLeft = Date.parse(expiresAt) - Date.now();
        const threshold = 24 * 60 * 60 * 1000; // 24h
        if (Number.isFinite(msLeft) && msLeft > 0 && msLeft < threshold) {
          try {
            const fresh = await api<TokenResponse>("/api/auth/refresh", {
              method: "POST",
              token: token.celmisToken as string,
            });
            token.celmisToken = fresh.access_token;
            token.celmisExpiresAt = fresh.expires_at;
            token.meCheckedAt = 0; // re-read the user below with the new token
          } catch {
            // keep the old token — natural expiry will log the user out
          }
        }
      }

      // is_admin used to be copied once at sign-in, so a promotion or a
      // demotion (CLI, admin page, IdP role on the next SSO login) did not
      // reach the UI for up to 30 days. The backend re-reads it per request
      // anyway; this keeps the session's copy at most ME_RECHECK_MS stale.
      if (token.celmisToken && Date.now() - (token.meCheckedAt ?? 0) > ME_RECHECK_MS) {
        try {
          const me = await api<UserOut>("/api/auth/me", { token: token.celmisToken as string });
          token.isAdmin = me.is_admin;
          token.isSuperadmin = Boolean(me.is_superadmin);
          token.meCheckedAt = Date.now();
        } catch {
          // backend unreachable or token refused — keep the last known value
        }
      }
      return token;
    },
    session: async ({ session, token }) => {
      session.user = {
        ...session.user,
        id: (token.userId as string | undefined) ?? "",
        email: (token.email as string | undefined) ?? session.user.email,
        name: (token.name as string | undefined) ?? session.user.name ?? "",
      };
      session.celmisToken = (token.celmisToken as string | undefined) ?? null;
      session.celmisExpiresAt = (token.celmisExpiresAt as string | undefined) ?? null;
      session.isAdmin = Boolean(token.isAdmin);
      session.isSuperadmin = Boolean(token.isSuperadmin);
      return session;
    },
  },
});
