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

/** A calendar day with no time and no zone: "2026-10-01". */
const CALENDAR_DAY = /^\d{4}-\d{2}-\d{2}$/;

/** Date-only variant of formatDateTime.
 *
 *  A bare calendar day ("2026-10-01", what the API sends for a daily bucket)
 *  is a day, not an instant. `new Date()` reads it as UTC midnight, and
 *  formatting that in local time put every day one day early west of UTC —
 *  New York saw 30 Sep for the 1 Oct bucket. So a calendar day is formatted
 *  in UTC, where it was parsed, and comes back as the same day everywhere. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const opts: Intl.DateTimeFormatOptions = CALENDAR_DAY.test(iso)
    ? { dateStyle: "short", timeZone: "UTC" }
    : { dateStyle: "short" };
  try {
    return d.toLocaleDateString(resolveLocale(), opts);
  } catch {
    return d.toLocaleDateString(undefined, CALENDAR_DAY.test(iso) ? { timeZone: "UTC" } : undefined);
  }
}
