import { NextResponse } from "next/server";
import { auth } from "@/auth";

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
  if (isOpen) return NextResponse.next();
  if (!isLoggedIn && !isAuthPage) {
    const url = new URL("/login", req.nextUrl);
    url.searchParams.set("from", pathname);
    return NextResponse.redirect(url);
  }
  if (isLoggedIn && isAuthPage) {
    return NextResponse.redirect(new URL("/dashboard", req.nextUrl));
  }
  return NextResponse.next();
});

// Run middleware on all routes except API/auth/static
export const config = {
  // The PWA assets have to be exempt, not merely "open": a service worker
  // that answers 307 never registers, so a redirect here disables push
  // entirely — and the manifest and icons are fetched by the browser
  // itself, outside any session, when installing to the Home Screen.
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|sw\\.js|manifest\\.webmanifest|icon-\\d+\\.png).*)",
  ],
};
