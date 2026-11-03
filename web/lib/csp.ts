/**
 * Content-Security-Policy and the other response headers of the web app.
 *
 * A pure module (no `next/*` imports) so the proxy, the next.config headers and
 * the tests all read one definition.
 *
 * The policy is built per request, because a script nonce must be fresh for
 * every response. `'strict-dynamic'` lets the nonce'd bootstrap scripts that
 * Next injects load the rest of the bundle, and makes the browser ignore host
 * and `'unsafe-inline'` entries in `script-src`, so no inline script runs
 * without the nonce and no `unsafe-eval` is allowed outside development.
 *
 * `style-src` keeps `'unsafe-inline'` on purpose: React and the animation
 * library render `style="…"` attributes into the HTML itself, and CSP cannot
 * allow style attributes by nonce. Styles do not execute code; the script
 * policy is what carries the weight.
 *
 * `CELMIS_CSP` is the safety valve: `enforce` (default), `report-only` (the
 * browser logs violations and blocks nothing) or `off`. It is read at request
 * time, so changing the variable and restarting the container reverts a
 * production breakage without a release.
 */

export type CspMode = "enforce" | "report-only" | "off";

export function cspMode(raw: string | undefined): CspMode {
  const v = (raw ?? "").trim().toLowerCase();
  return v === "report-only" || v === "off" ? v : "enforce";
}

export type CspInput = {
  nonce: string;
  /** `next dev` needs `unsafe-eval` (React debugging) and a websocket (HMR). */
  isDev?: boolean;
  /** AUTH_OIDC_ISSUER: NextAuth's sign-in answers a POST with a redirect to
   *  the provider, and browsers apply `form-action` to redirects too. */
  oidcIssuer?: string;
  /** Google sign-in is the other provider that redirects away. */
  googleEnabled?: boolean;
  /** NEXT_PUBLIC_API_BASE. Relative (`/backend`) means same origin. */
  apiBase?: string;
};

/** `https://idp.example.com` from any URL-ish string, or null. */
export function originOf(value: string | undefined): string | null {
  const raw = (value ?? "").trim();
  if (!raw) return null;
  try {
    const u = new URL(raw);
    return u.protocol === "http:" || u.protocol === "https:" ? u.origin : null;
  } catch {
    return null;
  }
}

export function buildCsp(input: CspInput): string {
  const { nonce, isDev = false } = input;
  const issuer = originOf(input.oidcIssuer);
  const api = originOf(input.apiBase); // null for "/backend" and for unset

  const formAction = ["'self'"];
  if (issuer) formAction.push(issuer);
  if (input.googleEnabled) formAction.push("https://accounts.google.com");

  const connect = ["'self'"];
  if (api) connect.push(api);
  if (isDev) connect.push("ws:", "wss:");

  const script = ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'"];
  if (isDev) script.push("'unsafe-eval'");

  const directives: [string, string[]][] = [
    ["default-src", ["'self'"]],
    ["script-src", script],
    ["style-src", ["'self'", "'unsafe-inline'"]],
    ["img-src", ["'self'", "data:", "blob:"]],
    ["font-src", ["'self'", "data:"]],
    ["connect-src", connect],
    ["worker-src", ["'self'", "blob:"]],
    ["manifest-src", ["'self'"]],
    ["media-src", ["'self'", "blob:"]],
    ["frame-src", ["'none'"]],
    ["object-src", ["'none'"]],
    ["base-uri", ["'self'"]],
    ["form-action", formAction],
    ["frame-ancestors", ["'none'"]],
  ];
  return directives.map(([name, values]) => `${name} ${values.join(" ")}`).join("; ");
}

/** The CSP header (name and value) for a mode, or null when it is off. */
export function cspHeader(mode: CspMode, policy: string): [string, string] | null {
  if (mode === "off") return null;
  return [
    mode === "report-only" ? "Content-Security-Policy-Report-Only" : "Content-Security-Policy",
    policy,
  ];
}

/** 128 bits from the platform CSPRNG, base64 — fresh for every response. */
export function makeNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

/** Sent on every response, whatever the CSP mode. */
export const STATIC_SECURITY_HEADERS: [string, string][] = [
  ["X-Content-Type-Options", "nosniff"],
  ["Referrer-Policy", "strict-origin-when-cross-origin"],
  [
    "Permissions-Policy",
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()",
  ],
  ["X-Frame-Options", "DENY"],
];

/** Only a response that went out over https says so: browsers ignore the
 *  header on http, but a plain-http dev server should not claim it either. */
export const HSTS: [string, string] = ["Strict-Transport-Security", "max-age=31536000"];

export function isHttps(requestUrl: string, forwardedProto: string | null | undefined): boolean {
  const proto = (forwardedProto ?? "").split(",")[0].trim().toLowerCase();
  if (proto) return proto === "https";
  return requestUrl.startsWith("https:");
}
