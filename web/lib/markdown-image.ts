/**
 * How an image in markdown is shown when it must not be loaded.
 *
 * No imports, so a test can run it on its own. The text is the alt text, or —
 * when there is none — the address's host or file name, so the reader can tell
 * what was there. `href` is set only for an absolute http(s) address, the only
 * kind a click can follow; a relative path, `data:` or anything else gets text
 * alone.
 */
export function imageAsText(src: string, alt: string): { text: string; href: string | null } {
  const label = alt.replace(/\s+/g, " ").trim();
  let url: URL | null = null;
  try {
    url = new URL(src);
  } catch {
    url = null;
  }
  const web = url && (url.protocol === "http:" || url.protocol === "https:") ? url : null;
  if (label) return { text: label, href: web ? web.href : null };
  if (!web) return { text: "", href: null };
  const file = web.pathname.split("/").filter(Boolean).pop();
  return { text: file ? decodeURIComponentSafe(file) : web.host, href: web.href };
}

function decodeURIComponentSafe(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
