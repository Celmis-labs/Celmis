/**
 * Whether an href written by the model may become a link: a path on THIS
 * origin, and nothing a browser would read as another site.
 *
 * `startsWith("/") && !startsWith("//")` was the old test, and `/\evil.com`
 * passes it: a browser reads `\` as `/`, so the anchor resolves to
 * `https://evil.com/`. Tabs and newlines are stripped by the URL parser, so
 * `/\t/evil.com` is `//evil.com` too. The check is therefore the browser's
 * own: resolve against a fixed origin and require the origin to survive.
 *
 * No imports, so the tests can compile this file on its own.
 */
const PROBE_ORIGIN = "https://in-app.invalid";

export function isInAppHref(href: string | null | undefined): href is string {
  if (!href || !href.startsWith("/") || href.startsWith("//")) return false;
  if (/[\\\s\u0000-\u001f]/.test(href)) return false;
  try {
    return new URL(href, PROBE_ORIGIN).origin === PROBE_ORIGIN;
  } catch {
    return false;
  }
}
