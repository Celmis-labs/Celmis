"use client";

import { useSyncExternalStore } from "react";
import { MoonIcon, SunIcon } from "lucide-react";

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** Inline script (injected in <head>) that applies the saved theme before
 * first paint to avoid a flash of the wrong theme. */
export const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem('theme');var d=t==='dark'||(t==null&&window.matchMedia&&window.matchMedia('(prefers-color-scheme:dark)').matches);document.documentElement.classList.toggle('dark',!!d);}catch(e){}})();`;

/** The `dark` class on <html> is the store: the init script sets it before
 *  React exists, and the sidebar and the rail each render a toggle, so the
 *  state cannot live in one of them. Read with the hook meant for a store
 *  outside React rather than copied into state by a mount effect — which
 *  rendered "Light" on every first paint of a dark page and left two toggles
 *  free to disagree. */
function subscribeTheme(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  return () => observer.disconnect();
}

function readDark(): boolean {
  return document.documentElement.classList.contains("dark");
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const t = useT();
  const dark = useSyncExternalStore(subscribeTheme, readDark, () => false);
  const toggle = () => {
    const next = !dark;
    document.documentElement.classList.toggle("dark", next);
    try { localStorage.setItem("theme", next ? "dark" : "light"); } catch {}
  };
  return (
    <button
      type="button"
      onClick={toggle}
      title={t("shell.theme")}
      aria-label={t("shell.theme")}
      aria-pressed={dark}
      className={cn(
        "inline-flex items-center gap-1.5 rounded text-xs text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]",
        compact
          ? "size-9 justify-center rounded-md"
          : "min-h-11 px-2 py-0.5 sm:min-h-0 sm:px-1.5 sm:text-[11px]",
      )}
    >
      {dark ? <MoonIcon className="h-3.5 w-3.5" /> : <SunIcon className="h-3.5 w-3.5" />}
      {!compact && (dark ? t("shell.dark") : t("shell.light"))}
    </button>
  );
}
