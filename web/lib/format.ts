/**
 * Locale-aware date/time formatting.
 *
 * Replaces the ad-hoc `iso.slice(0, 19).replace("T", " ")` pattern that was
 * copy-pasted across pages. Uses the browser locale when available (client
 * components only render these after data fetches, so `navigator` exists),
 * falling back to uk-UA.
 */

function resolveLocale(): string {
  if (typeof navigator !== "undefined" && navigator.language) {
    return navigator.language;
  }
  return "uk-UA";
}

/** "10.08.26, 14:32" (locale-dependent). Returns "—" for empty input. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  try {
    return d.toLocaleString(resolveLocale(), { dateStyle: "short", timeStyle: "short" });
  } catch {
    return d.toLocaleString();
  }
}

/** Date-only variant of formatDateTime. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  try {
    return d.toLocaleDateString(resolveLocale(), { dateStyle: "short" });
  } catch {
    return d.toLocaleDateString();
  }
}
