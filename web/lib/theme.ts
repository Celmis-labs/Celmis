/**
 * Light or dark, decided in one place.
 *
 * The `dark` class on <html> is the only thing the styles read (globals.css
 * maps Tailwind's `dark:` variant to it), so everything here is about getting
 * that one class right: before the first paint, when the system's preference
 * changes, and when another tab changes the saved one.
 *
 * A plain module, not part of `theme-toggle.tsx`: the root layout is a server
 * component and needs `THEME_INIT_SCRIPT` as a string, which a client module
 * does not give it.
 */

export type Theme = "light" | "dark";

/** localStorage key of the saved choice. Written only by the toggle, so a
 *  visitor who never pressed it has none, and follows the system. */
export const THEME_KEY = "theme";

export const DARK_QUERY = "(prefers-color-scheme: dark)";

/** The theme to show: a saved "light" or "dark" always wins; no saved choice,
 *  or anything that is not one of the two, follows the system. */
export function resolveTheme(saved: string | null | undefined, systemDark: boolean): Theme {
  if (saved === "light" || saved === "dark") return saved;
  return systemDark ? "dark" : "light";
}

/** What the theme code needs from a browser window. Narrow on purpose: it is
 *  what a test can fake without a DOM, and it keeps this module from reaching
 *  for globals it was not handed. A real `window` satisfies it. */
export type ThemeEnv = {
  matchMedia(query: string): {
    matches: boolean;
    addEventListener(type: "change", listener: () => void): void;
    removeEventListener(type: "change", listener: () => void): void;
  };
  addEventListener(type: "storage", listener: () => void): void;
  removeEventListener(type: "storage", listener: () => void): void;
  localStorage: Pick<Storage, "getItem">;
  document: { documentElement: { classList: { toggle(token: string, force: boolean): unknown } } };
};

/** The saved choice, or null — also when storage is blocked (a private window,
 *  site data switched off), where merely touching it throws. */
export function savedTheme(env: Pick<ThemeEnv, "localStorage"> = window): string | null {
  try {
    return env.localStorage.getItem(THEME_KEY);
  } catch {
    return null;
  }
}

export function applyTheme(theme: Theme, env: Pick<ThemeEnv, "document"> = window): void {
  env.document.documentElement.classList.toggle("dark", theme === "dark");
}

/** Keeps the page's theme in step with what decides it after the first paint;
 *  returns the function that stops. Does nothing where `matchMedia` is missing.
 *
 *  With no saved choice the theme IS the system's, so it has to move when the
 *  system does: a laptop that goes dark at sunset, a phone with a scheduled
 *  appearance. A saved choice makes the listener a no-op, because
 *  `resolveTheme` lets it win. `storage` covers the other tab: pressing the
 *  toggle there changes the saved choice, and this tab would otherwise keep
 *  the old one until it was reloaded. */
export function followSystemTheme(env: ThemeEnv = window): () => void {
  let query: ReturnType<ThemeEnv["matchMedia"]>;
  try {
    query = env.matchMedia(DARK_QUERY);
  } catch {
    return () => {};
  }
  const sync = () => applyTheme(resolveTheme(savedTheme(env), query.matches), env);
  query.addEventListener("change", sync);
  env.addEventListener("storage", sync);
  return () => {
    query.removeEventListener("change", sync);
    env.removeEventListener("storage", sync);
  };
}

/** Runs inline in <head>, before anything paints, so a page never shows the
 *  wrong theme for a frame. It cannot import: it is the same decision as
 *  `resolveTheme`, written out as ES5 text, and `theme.test.ts` runs it
 *  against `resolveTheme` for every combination so the two cannot drift.
 *
 *  Each read is guarded on its own: blocked storage must still leave the
 *  system's preference in force, not a light page on a dark system. */
export const THEME_INIT_SCRIPT = `(function(){var d=false;try{d=window.matchMedia('${DARK_QUERY}').matches;}catch(e){}try{var t=localStorage.getItem('${THEME_KEY}');if(t==='dark'||t==='light')d=t==='dark';}catch(e){}document.documentElement.classList.toggle('dark',d);})();`;
