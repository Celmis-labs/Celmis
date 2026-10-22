/**
 * Which languages the interface speaks, and how one is chosen for a visitor
 * who has not chosen.
 *
 * A plain module on purpose, not part of `index.tsx`. That file is a client
 * module, and a server component (the root layout) importing a constant or a
 * function from a client module does not get the value, it gets a reference to
 * it. The layout has to run this code on the server to paint the first byte in
 * the right language, so it lives where both sides can import it.
 *
 * Nothing here writes anything. A language picked by detection is the
 * browser's choice, not the person's: it must not become a saved preference,
 * or changing the browser's language later would silently stop working. Only
 * the switcher (`setLocale` in `index.tsx`) saves.
 */

export type Locale =
  | "en" | "uk" | "de" | "fr" | "es" | "it" | "pt" | "pl" | "nl"
  | "cs" | "sk" | "ro" | "tr" | "ja" | "ko" | "zh";

/** Languages with a COMPLETE dictionary in `messages/` — only these are
 *  offered in the switcher and only these can be detected. Adding a file means
 *  adding its code here; showing a language without a translation is a silent
 *  English fallback. `tests/web/test_the_interface_starts_in_the_browsers_
 *  language_and_theme.py` holds this list to the files. */
export const LOCALES: Locale[] = [
  "en", "uk", "de", "es", "fr", "it", "nl", "pl", "pt",
  "cs", "sk", "ro", "tr", "ja", "ko", "zh",
];

export const DEFAULT_LOCALE: Locale = "en";

/** Whether `value` is, exactly, one of the interface's locale codes. */
export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as string[]).includes(value);
}

/** The locale a BCP 47 tag belongs to, or null.
 *
 *  Only the primary subtag counts: "uk-UA", "pt-BR", "zh-Hans-CN", "zh-TW",
 *  "PT_br" and "uk" all name a language there is a dictionary for, and the
 *  region or script is a refinement the interface does not have. "", "*" and
 *  a language with no dictionary ("sv", "nb", "i-klingon") are no match, so the
 *  caller moves on to the next thing the browser prefers. */
export function localeOfTag(tag: string | null | undefined): Locale | null {
  const primary = String(tag ?? "").trim().toLowerCase().split(/[-_]/)[0];
  return isLocale(primary) ? primary : null;
}

/** An `Accept-Language` header as language tags, most preferred first.
 *
 *  Ordered by q-value; equal q keeps the order of the header, which is how
 *  browsers list their languages. A tag with `q=0` is one the browser refuses,
 *  so it is dropped; a q that is not a number is read as 1 rather than losing
 *  the tag to somebody's typo. Capped, because the header is client input
 *  and only the first few entries can ever matter. */
export function parseAcceptLanguage(header: string | null | undefined): string[] {
  const entries: { tag: string; q: number }[] = [];
  for (const part of String(header ?? "").split(",").slice(0, 32)) {
    const [rawTag, ...params] = part.split(";");
    const tag = rawTag.trim();
    if (!tag) continue;
    let q = 1;
    for (const param of params) {
      const m = /^\s*q\s*=\s*(.*?)\s*$/i.exec(param);
      if (!m) continue;
      // Number("") is 0, which would read a bare "q=" as a refusal.
      const n = m[1] === "" ? NaN : Number(m[1]);
      q = Number.isFinite(n) ? Math.min(Math.max(n, 0), 1) : 1;
    }
    if (q > 0) entries.push({ tag, q });
  }
  // Array.prototype.sort is stable, so ties keep the header's order.
  return entries.sort((a, b) => b.q - a.q).map((e) => e.tag);
}

/** The best locale for a list of preferred languages, most preferred first —
 *  `navigator.languages`, or `parseAcceptLanguage(header)`.
 *
 *  The first preference that has a dictionary wins, so ["sv", "de-AT", "en"]
 *  is German: Swedish is skipped because there is nothing to show in it, not
 *  because the visitor asked for English. Nothing matching is English. */
export function pickLocale(preferred: readonly string[] | null | undefined): Locale {
  for (const tag of preferred ?? []) {
    const locale = localeOfTag(tag);
    if (locale) return locale;
  }
  return DEFAULT_LOCALE;
}

/** Where a first-paint locale came from.
 *
 *  - "saved":   the visitor picked it in the switcher (the `locale` cookie).
 *  - "browser": nothing was saved, so the browser's `Accept-Language` decided
 *               (English included, when none of its languages has a
 *               dictionary).
 *  - "none":    nothing was saved and the request carried no preference — a
 *               proxy that strips the header, a bot. The page falls back to
 *               English and lets the client ask `navigator.languages`. */
export type LocaleSource = "saved" | "browser" | "none";

/** The locale to render the first byte in. A saved choice always wins, and a
 *  cookie that is not a locale (an old value, a hand edit) is not a choice. */
export function detectLocale(
  saved: string | null | undefined,
  acceptLanguage: string | null | undefined,
): { locale: Locale; source: LocaleSource } {
  if (isLocale(saved)) return { locale: saved, source: "saved" };
  const preferred = parseAcceptLanguage(acceptLanguage);
  if (preferred.length === 0) return { locale: DEFAULT_LOCALE, source: "none" };
  return { locale: pickLocale(preferred), source: "browser" };
}
