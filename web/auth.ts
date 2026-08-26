/**
 * NextAuth (Auth.js v5) configuration.
 *
 * Two providers:
 *   - Credentials (email + password) → calls FastAPI /api/auth/login
 *   - Google     → calls FastAPI /api/auth/google with the Google id_token
 *
 * The FastAPI backend is the source of truth for the user record. NextAuth
 * stores the backend-issued JWT inside the session so frontend pages can
 * forward it as Bearer to FastAPI.
 */
import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";
import Google from "next-auth/providers/google";

import { api, type TokenResponse, type UserOut } from "@/lib/api";

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
  ],
  callbacks: {
    jwt: async ({ token, user, account }) => {
      // Initial sign-in via Credentials provider — token comes back from authorize()
      if (user && (user as { celmisToken?: string }).celmisToken) {
        const u = user as { id: string; email: string; name: string; celmisToken: string; celmisExpiresAt: string; isAdmin: boolean };
        token.celmisToken = u.celmisToken;
        token.celmisExpiresAt = u.celmisExpiresAt;
        token.isAdmin = u.isAdmin;
        token.userId = u.id;
        return token;
      }

      // Initial sign-in via Google — exchange id_token for our JWT
      if (account?.provider === "google" && account.id_token) {
        try {
          const tok = await api<TokenResponse>("/api/auth/google", {
            method: "POST",
            json: { id_token: account.id_token },
          });
          const me = await api<UserOut>("/api/auth/me", { token: tok.access_token });
          token.celmisToken = tok.access_token;
          token.celmisExpiresAt = tok.expires_at;
          token.userId = me.id;
          token.isAdmin = me.is_admin;
          token.email = me.email;
          token.name = me.name || me.email;
        } catch {
          // backend rejected — drop celmis fields so middleware logs out
          delete token.celmisToken;
        }
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
          } catch {
            // keep the old token — natural expiry will log the user out
          }
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
      return session;
    },
  },
});
