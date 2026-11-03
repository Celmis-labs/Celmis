import { type NextRequest, NextResponse } from "next/server";
import { getToken } from "next-auth/jwt";

import { buildEndSessionUrl, postLogoutRedirectUri } from "@/lib/federated-logout";

/**
 * POST → `{ url }`: where to send the browser to end the identity provider's
 * session, or `{ url: null }` when there is nothing to end there (password,
 * master or Google sign-in; a provider without an end-session endpoint).
 *
 * Read from the encrypted session cookie on the server: the id_token it needs
 * for `id_token_hint` is never part of the session the browser can read. The
 * caller then runs `signOut({ redirect: false })` and navigates to `url`.
 *
 * POST, not GET: it hands out a URL carrying the id_token, so it must not be
 * reachable by an image tag or a link on another site.
 */
export async function POST(req: NextRequest) {
  const none = NextResponse.json({ url: null }, { headers: { "Cache-Control": "no-store" } });
  const issuer = process.env.AUTH_OIDC_ISSUER;
  const clientId = process.env.AUTH_OIDC_CLIENT_ID;
  const secret = process.env.AUTH_SECRET ?? process.env.NEXTAUTH_SECRET;
  if (!issuer || !clientId || !secret) return none;

  // Auth.js names the cookie after the transport it was set over, and uses the
  // name as the encryption salt.
  const secure = (req.headers.get("x-forwarded-proto") ?? req.nextUrl.protocol).startsWith("https");
  let token = null;
  for (const useSecure of secure ? [true, false] : [false, true]) {
    const salt = `${useSecure ? "__Secure-" : ""}authjs.session-token`;
    token = await getToken({ req, secret, secureCookie: useSecure, salt }).catch(() => null);
    if (token) break;
  }
  const idToken = typeof token?.idToken === "string" ? token.idToken : "";
  if (!idToken) return none;

  const url = await buildEndSessionUrl({
    issuer,
    clientId,
    idToken,
    postLogoutRedirectUri: postLogoutRedirectUri(process.env, req.headers, req.nextUrl.origin),
  });
  return NextResponse.json({ url }, { headers: { "Cache-Control": "no-store" } });
}
