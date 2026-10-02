/** The sidebar's open state lives in a cookie as well as on the page, because
 *  only a cookie reaches the server. It was localStorage, read in an effect:
 *  every load rendered the full 240px column, painted it, and then collapsed
 *  it to the rail a frame later — the flash on every navigation for anybody
 *  who preferred the rail. The (app) layout reads this cookie and hands the
 *  value down, so the first HTML is already the right width.
 *
 *  A plain module rather than an export of app-shell.tsx: that file is a
 *  client module, and a constant imported from one into a server component
 *  arrives as a client reference, not as the string. */
export const SIDEBAR_COOKIE = "celmis-sidebar";

/** Where the state lived before the cookie. Read once, by a browser that has
 *  no cookie yet, so somebody who collapsed the sidebar before this shipped
 *  does not find it expanded again; then removed — the cookie is the only
 *  record from there on. */
export const LEGACY_SIDEBAR_KEY = "celmis:sidebar";

export function writeSidebarCookie(value: "open" | "closed"): void {
  document.cookie = `${SIDEBAR_COOKIE}=${value}; path=/; max-age=31536000; SameSite=Lax`;
}
