"use client";

/**
 * Lightweight i18n for the App Router — no external dependency.
 *
 * Both dictionaries are bundled, so switching language is instant (no reload,
 * no network). The chosen locale is persisted in a `locale` cookie and read
 * server-side in the root layout to avoid a flash of the wrong language. With
 * no cookie the browser's own preference decides — `Accept-Language` on the
 * server, `navigator.languages` as the fallback below — and that is never
 * written back: only an explicit pick in the switcher is a saved choice. The
 * list of locales and the matching live in `./locales`, a plain module the
 * server can import.
 *
 * Usage:
 *   const t = useT();
 *   <h1>{t("nav.dashboard")}</h1>
 *   <p>{t("chat.filesHidden", { count: 3 })}</p>   // {count} interpolation
 */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from "react";

import en from "./messages/en.json";
import uk from "./messages/uk.json";
import { DEFAULT_LOCALE, LOCALES, localeOfTag, pickLocale, type Locale } from "./locales";

export { DEFAULT_LOCALE, LOCALES };
export type { Locale };

// en + uk are bundled (instant switching for the main two); the rest are
// loaded dynamically when chosen — otherwise sixteen dictionaries would add
// ~1 MB to the client bundle of every user.

// Native names for the switcher.
export const LOCALE_NAMES: Record<Locale, string> = {
  en: "English", uk: "Українська", de: "Deutsch", fr: "Français",
  es: "Español", it: "Italiano", pt: "Português", pl: "Polski",
  nl: "Nederlands", cs: "Čeština", sk: "Slovenčina", ro: "Română",
  tr: "Türkçe", ja: "日本語", ko: "한국어", zh: "中文",
};

const STATIC_DICTS: Partial<Record<Locale, Record<string, string>>> = {
  en: en as Record<string, string>,
  uk: uk as Record<string, string>,
};

// Dictionaries fetched for a language nobody is browsing in — see
// `useDictFor`. Module level, not component state: a thread can hold four
// replies in the same language, and a dictionary is ~170 KB of JSON that
// should cross the network once per sitting rather than once per reply.
const namedDicts: Partial<Record<Locale, Record<string, string>>> = {};
const namedLoading = new Set<Locale>();
const namedListeners = new Set<() => void>();

function loadNamedDict(locale: Locale): void {
  if (STATIC_DICTS[locale] || namedDicts[locale] || namedLoading.has(locale)) return;
  namedLoading.add(locale);
  import(`./messages/${locale}.json`)
    .then((m) => { namedDicts[locale] = m.default as Record<string, string>; })
    .catch(() => { /* нема файлу — лишається фолбек на активну мову */ })
    .finally(() => {
      namedLoading.delete(locale);
      // An arrival is a module event; this is what makes it a render.
      namedListeners.forEach((fn) => fn());
    });
}

/** An ISO 639-1 code as a locale there is a dictionary for, or nothing.
 *
 *  "" (the planner could not tell), "sp", "en-GB" and every other unknown
 *  code mean the same thing to the caller: no named dictionary, so the
 *  interface language stands.
 */
function asLocale(code: string | null | undefined): Locale | null {
  return localeOfTag(code);
}

type I18nCtx = {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
};

const Ctx = createContext<I18nCtx | null>(null);

function interpolate(str: string, vars?: Record<string, string | number>): string {
  if (!vars) return str;
  return str.replace(/\{(\w+)\}/g, (_m, k) => (k in vars ? String(vars[k]) : `{${k}}`));
}

export function I18nProvider({
  initialLocale = DEFAULT_LOCALE,
  initialDict,
  askBrowser = false,
  children,
}: {
  initialLocale?: Locale;
  /** The dictionary of `initialLocale` when the client bundle does not carry
   *  it (anything but en/uk). The server loads it, so the first render — the
   *  HTML — is in the visitor's language instead of English until a download
   *  finishes. */
  initialDict?: Record<string, string>;
  /** The server had neither a saved choice nor an `Accept-Language` to go on
   *  (see `LocaleSource` "none"), so `initialLocale` is only the fallback and
   *  the browser is asked once, here. */
  askBrowser?: boolean;
  children: React.ReactNode;
}) {
  const [locale, setLocaleState] = useState<Locale>(
    LOCALES.includes(initialLocale) ? initialLocale : DEFAULT_LOCALE,
  );
  // Динамічно довантажені словники (усі мови, крім en/uk).
  const [lazyDicts, setLazyDicts] = useState<
    Partial<Record<Locale, Record<string, string>>>
  >(() => (
    initialDict && LOCALES.includes(initialLocale) && !STATIC_DICTS[initialLocale]
      ? { [initialLocale]: initialDict }
      : {}
  ));

  useEffect(() => {
    if (STATIC_DICTS[locale] || lazyDicts[locale]) return;
    let cancelled = false;
    import(`./messages/${locale}.json`)
      .then((m) => {
        if (!cancelled) {
          setLazyDicts((d) => ({ ...d, [locale]: m.default as Record<string, string> }));
        }
      })
      .catch(() => { /* нема файлу — лишаємось на en-фолбеку */ });
    return () => { cancelled = true; };
  }, [locale, lazyDicts]);

  // Not `setLocale`: nobody chose this language, the browser did, so it is
  // neither saved nor sticky. The next visit asks the browser again.
  useEffect(() => {
    if (!askBrowser) return;
    const preferred = navigator.languages?.length ? navigator.languages : [navigator.language];
    const detected = pickLocale(preferred);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- a one-shot read of a value only the browser has
    setLocaleState(detected);
    document.documentElement.lang = detected;
  }, [askBrowser]);

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    try {
      document.cookie = `locale=${l}; path=/; max-age=31536000; SameSite=Lax`;
      document.documentElement.lang = l;
    } catch {
      /* SSR / no document — ignore */
    }
  }, []);

  const t = useCallback(
    (key: string, vars?: Record<string, string | number>) => {
      const dict = STATIC_DICTS[locale] ?? lazyDicts[locale]
        ?? STATIC_DICTS[DEFAULT_LOCALE]!;
      const val = dict[key] ?? (STATIC_DICTS.en as Record<string, string>)[key] ?? key;
      return interpolate(val, vars);
    },
    [locale, lazyDicts],
  );

  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18nCtx {
  const ctx = useContext(Ctx);
  if (!ctx) {
    // Fallback so a component used outside the provider still renders keys.
    return {
      locale: DEFAULT_LOCALE,
      setLocale: () => {},
      t: (k: string, vars?: Record<string, string | number>) =>
        interpolate(((STATIC_DICTS.en as Record<string, string>)[k] ?? k), vars),
    };
  }
  return ctx;
}

/** Convenience — returns just the `t` function. */
export function useT() {
  return useI18n().t;
}

/** A `t()` for a NAMED language, rather than the one the interface is in.
 *
 *  The canned parts of a reply — the capabilities message, the product
 *  description, the empty answers — exist in sixteen languages precisely so
 *  that saying them costs no model tokens. Rendering them in the interface
 *  language throws that away: somebody asks in Ukrainian, the model answers
 *  in Ukrainian, and underneath it sits an English panel. So the CONTENT of a
 *  reply is looked up in the language the question was written in, which the
 *  planner reports per run.
 *
 *  Only the content. Buttons, the session list and column headings stay in
 *  the interface language — the person chose that, and a Send button that
 *  changed language per message would be absurd.
 *
 *  Falls back to the active locale, never to a raw key: while the dictionary
 *  is still in flight, when the code is empty because the planner could not
 *  establish one, and when it names a language there is no dictionary for.
 */
export function useDictFor(code: string | null | undefined) {
  const { locale, t: active } = useI18n();
  const target = asLocale(code);
  const [, redraw] = useState(0);

  useEffect(() => {
    // The active locale is the provider's own business; asking for it here
    // would fetch the same dictionary a second time.
    if (!target || target === locale) return;
    if (STATIC_DICTS[target] || namedDicts[target]) return;
    const onLoaded = () => redraw((n) => n + 1);
    namedListeners.add(onLoaded);
    loadNamedDict(target);
    return () => { namedListeners.delete(onLoaded); };
  }, [target, locale]);

  return useCallback(
    (key: string, vars?: Record<string, string | number>) => {
      if (!target || target === locale) return active(key, vars);
      const dict = STATIC_DICTS[target] ?? namedDicts[target];
      if (!dict) return active(key, vars);
      const val = dict[key] ?? (STATIC_DICTS.en as Record<string, string>)[key] ?? key;
      return interpolate(val, vars);
    },
    [target, locale, active],
  );
}
