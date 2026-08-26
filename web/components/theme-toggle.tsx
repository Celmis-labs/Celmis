"use client";

import { useEffect, useState } from "react";
import { MoonIcon, SunIcon } from "lucide-react";

import { useT } from "@/lib/i18n";

/** Inline script (injected in <head>) that applies the saved theme before
 * first paint to avoid a flash of the wrong theme. */
export const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem('theme');var d=t==='dark'||(t==null&&window.matchMedia&&window.matchMedia('(prefers-color-scheme:dark)').matches);document.documentElement.classList.toggle('dark',!!d);}catch(e){}})();`;

export function ThemeToggle() {
  const t = useT();
  const [dark, setDark] = useState(false);
  useEffect(() => {
    setDark(document.documentElement.classList.contains("dark"));
  }, []);
  const toggle = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    try { localStorage.setItem("theme", next ? "dark" : "light"); } catch {}
  };
  return (
    <button
      onClick={toggle}
      title={t("shell.theme")}
      aria-label={t("shell.theme")}
      className="inline-flex items-center gap-1.5 min-h-11 rounded px-2 py-0.5 text-xs text-[var(--color-muted-foreground)] sm:min-h-0 sm:px-1.5 sm:text-[11px] hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)] transition-colors"
    >
      {dark ? <MoonIcon className="h-3.5 w-3.5" /> : <SunIcon className="h-3.5 w-3.5" />}
      {dark ? t("shell.dark") : t("shell.light")}
    </button>
  );
}
