import vm from "node:vm";

import { describe, expect, it } from "vitest";

import {
  DARK_QUERY, THEME_INIT_SCRIPT, THEME_KEY, applyTheme, followSystemTheme, resolveTheme,
  savedTheme, type ThemeEnv,
} from "./theme";

type Saved = string | null | "storage throws";
type Media = boolean | "missing" | "throws";

/** A window just big enough for the theme code: a class list, a media query
 *  that can be flipped, a storage that can be blocked, and listeners that can be
 *  fired. */
function fakeWindow(opts: { saved?: Saved; media?: Media; dark?: boolean } = {}) {
  const { media = false } = opts;
  let saved: Saved = opts.saved ?? null;
  let systemDark = media === true;
  const classes = new Set<string>(opts.dark ? ["dark"] : []);
  const mediaListeners = new Set<() => void>();
  const windowListeners = new Set<() => void>();
  const queries: string[] = [];
  const keys: string[] = [];

  const win = {
    matchMedia(q: string) {
      queries.push(q);
      if (media === "throws") throw new Error("no matchMedia");
      return {
        get matches() { return systemDark; },
        addEventListener: (_t: "change", f: () => void) => { mediaListeners.add(f); },
        removeEventListener: (_t: "change", f: () => void) => { mediaListeners.delete(f); },
      };
    },
    addEventListener: (_t: "storage", f: () => void) => { windowListeners.add(f); },
    removeEventListener: (_t: "storage", f: () => void) => { windowListeners.delete(f); },
    get localStorage(): Pick<Storage, "getItem"> {
      if (saved === "storage throws") throw new Error("SecurityError");
      return { getItem: (k: string) => { keys.push(k); return saved as string | null; } };
    },
    document: {
      documentElement: {
        classList: {
          toggle(token: string, force: boolean) {
            if (force) classes.add(token); else classes.delete(token);
          },
        },
      },
    },
  };
  return {
    win: win as unknown as ThemeEnv,
    isDark: () => classes.has("dark"),
    listeners: () => mediaListeners.size + windowListeners.size,
    queries, keys,
    setSystem(dark: boolean) { systemDark = dark; mediaListeners.forEach((f) => f()); },
    setSaved(v: Saved) { saved = v; windowListeners.forEach((f) => f()); },
    /** The same window as the sandbox the inline script runs in. */
    sandbox() {
      const sb: Record<string, unknown> = { document: win.document, window: {} };
      if (media !== "missing") (sb.window as Record<string, unknown>).matchMedia = win.matchMedia;
      Object.defineProperty(sb, "localStorage", { get: () => win.localStorage });
      return sb;
    },
  };
}

const SAVED: Saved[] = [null, "light", "dark", "system", "", "DARK", "storage throws"];
const MEDIA: Media[] = [true, false, "missing", "throws"];

describe("resolveTheme", () => {
  it("lets a saved light or dark win over the system", () => {
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
  });

  it("follows the system when nothing is saved", () => {
    expect(resolveTheme(null, true)).toBe("dark");
    expect(resolveTheme(undefined, false)).toBe("light");
  });

  it("does not take a stored value that is neither light nor dark for a choice", () => {
    for (const v of ["system", "", "DARK", "auto", "1"]) {
      expect(resolveTheme(v, true), v).toBe("dark");
      expect(resolveTheme(v, false), v).toBe("light");
    }
  });
});

describe("THEME_INIT_SCRIPT", () => {
  it("reaches the same theme as resolveTheme for every saved value and every system", () => {
    for (const saved of SAVED) {
      for (const media of MEDIA) {
        for (const dark of [false, true]) {
          const w = fakeWindow({ saved, media, dark });
          vm.runInNewContext(THEME_INIT_SCRIPT, w.sandbox());
          const stored = saved === "storage throws" ? null : saved;
          const expected = resolveTheme(stored, media === true);
          expect(w.isDark() ? "dark" : "light", `saved=${saved} media=${media} dark=${dark}`)
            .toBe(expected);
        }
      }
    }
  });

  it("asks the system with the query and the key the rest of the app uses", () => {
    const w = fakeWindow({ saved: null, media: true });
    vm.runInNewContext(THEME_INIT_SCRIPT, w.sandbox());
    expect(w.queries).toEqual([DARK_QUERY]);
    expect(w.keys).toEqual([THEME_KEY]);
  });

  it("keeps a dark system dark when storage is blocked", () => {
    const w = fakeWindow({ saved: "storage throws", media: true });
    vm.runInNewContext(THEME_INIT_SCRIPT, w.sandbox());
    expect(w.isDark()).toBe(true);
  });

  it("does not need anything it cannot rely on: no listener, no import, no arrow", () => {
    expect(THEME_INIT_SCRIPT).not.toMatch(/=>|\bimport\b|\bexport\b|addEventListener/);
  });
});

describe("savedTheme", () => {
  it("reads the saved value, or null when there is none or storage is blocked", () => {
    expect(savedTheme(fakeWindow({ saved: "dark" }).win)).toBe("dark");
    expect(savedTheme(fakeWindow({ saved: null }).win)).toBeNull();
    expect(savedTheme(fakeWindow({ saved: "storage throws" }).win)).toBeNull();
  });
});

describe("applyTheme", () => {
  it("puts the dark class on and takes it off", () => {
    const w = fakeWindow();
    applyTheme("dark", w.win);
    expect(w.isDark()).toBe(true);
    applyTheme("light", w.win);
    expect(w.isDark()).toBe(false);
  });
});

describe("followSystemTheme", () => {
  it("moves the page when the system changes and nothing is saved", () => {
    const w = fakeWindow({ media: false });
    followSystemTheme(w.win);
    w.setSystem(true);
    expect(w.isDark()).toBe(true);
    w.setSystem(false);
    expect(w.isDark()).toBe(false);
  });

  it("leaves a saved choice alone when the system changes", () => {
    const w = fakeWindow({ media: false, saved: "light", dark: false });
    followSystemTheme(w.win);
    w.setSystem(true);
    expect(w.isDark()).toBe(false);

    const d = fakeWindow({ media: true, saved: "dark", dark: true });
    followSystemTheme(d.win);
    d.setSystem(false);
    expect(d.isDark()).toBe(true);
  });

  it("follows the system again when storage is blocked", () => {
    const w = fakeWindow({ media: false, saved: "storage throws" });
    followSystemTheme(w.win);
    w.setSystem(true);
    expect(w.isDark()).toBe(true);
  });

  it("follows another tab's choice", () => {
    const w = fakeWindow({ media: false, saved: null });
    followSystemTheme(w.win);
    w.setSaved("dark");
    expect(w.isDark()).toBe(true);
    w.setSaved("light");
    expect(w.isDark()).toBe(false);
  });

  it("stops listening when asked", () => {
    const w = fakeWindow({ media: false });
    const stop = followSystemTheme(w.win);
    expect(w.listeners()).toBe(2);
    stop();
    expect(w.listeners()).toBe(0);
    w.setSystem(true);
    expect(w.isDark()).toBe(false);
  });

  it("does nothing, and says nothing, where matchMedia does not work", () => {
    const w = fakeWindow({ media: "throws" });
    const stop = followSystemTheme(w.win);
    expect(w.listeners()).toBe(0);
    expect(() => stop()).not.toThrow();
  });
});
