import { NextResponse } from "next/server";
import { auth } from "@/auth";
import {
  buildCsp,
  cspHeader,
  cspMode,
  HSTS,
  isHttps,
  makeNonce,
  STATIC_SECURITY_HEADERS,
} from "@/lib/csp";

/** Pages that only make sense when signed OUT — a signed-in visitor is sent
 * to the dashboard instead. */
const AUTH_PATHS = new Set([
  "/login",
  "/signup",
  "/forgot-password",
]);

/** Pages reachable in BOTH states, with no redirect either way.
 *
 *  - /reset-password: the token is the authorisation, so it must work whether
 *    or not a session exists.
 *  - /invite/<token>: an invite is accepted *while signed in*, so bouncing a
 *    signed-in visitor to the dashboard would make the link unusable — while a
 *    signed-out visitor still needs to see what the link grants before logging
 *    in. Prefix-matched because the token is a path segment.
 */
const OPEN_PATHS = new Set(["/reset-password"]);
const OPEN_PREFIXES = ["/invite/"];

export default auth((req) => {
  // One nonce per response. Next reads it back from the request's CSP header
  // and stamps it on the scripts it injects; `x-nonce` is for our own layout.
  const nonce = makeNonce();
  const policy = buildCsp({
    nonce,
    isDev: process.env.NODE_ENV === "development",
    oidcIssuer: process.env.AUTH_OIDC_ISSUER,
    googleEnabled: Boolean(process.env.GOOGLE_CLIENT_ID && process.env.GOOGLE_CLIENT_SECRET),
    apiBase: process.env.NEXT_PUBLIC_API_BASE,
  });
  const csp = cspHeader(cspMode(process.env.CELMIS_CSP), policy);

  const requestHeaders = new Headers(req.headers);
  if (csp) {
    requestHeaders.set("x-nonce", nonce);
    requestHeaders.set(csp[0], csp[1]);
  }
  const pass = () => NextResponse.next({ request: { headers: requestHeaders } });
  const res = route(req, pass);

  if (csp) res.headers.set(csp[0], csp[1]);
  for (const [name, value] of STATIC_SECURITY_HEADERS) res.headers.set(name, value);
  if (isHttps(req.nextUrl.href, req.headers.get("x-forwarded-proto"))) {
    res.headers.set(HSTS[0], HSTS[1]);
  }
  return res;
});

function route(
  req: Parameters<Parameters<typeof auth>[0]>[0],
  pass: () => NextResponse,
): NextResponse {
  const { pathname } = req.nextUrl;
  const isLoggedIn = Boolean(req.auth?.celmisToken);
  const isAuthPage = AUTH_PATHS.has(pathname);
  const isOpen =
    OPEN_PATHS.has(pathname) || OPEN_PREFIXES.some((p) => pathname.startsWith(p));

  if (pathname === "/") {
    return NextResponse.redirect(
      new URL(isLoggedIn ? "/dashboard" : "/login", req.nextUrl),
    );
  }
  if (isOpen) return pass();
  if (!isLoggedIn && !isAuthPage) {
    const url = new URL("/login", req.nextUrl);
    url.searchParams.set("from", pathname);
    return NextResponse.redirect(url);
  }
  if (isLoggedIn && isAuthPage) {
    return NextResponse.redirect(new URL("/dashboard", req.nextUrl));
  }
  return pass();
}

// Run middleware on all routes except API/auth/static
export const config = {
  // The PWA assets have to be exempt, not merely "open": a service worker
  // that answers 307 never registers, so a redirect here disables push
  // entirely — and the manifest and icons are fetched by the browser
  // itself, outside any session, when installing to the Home Screen.
  //
  // `.well-known` is exempt for the same reason, and it is the one that was
  // breaking sign-in for agents: an MCP client looks for the OAuth discovery
  // documents there, with no session, and a redirect to /login gave it an HTML
  // page where it expected JSON. app/.well-known answers those requests itself.
  //
  // `robots.txt` is exempt so a crawler gets the file, not a redirect to /login.
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|robots\\.txt|sw\\.js|manifest\\.webmanifest|icon-\\d+\\.png|\\.well-known).*)",
  ],
};
