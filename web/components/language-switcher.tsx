"use client";

import { CheckIcon, GlobeIcon } from "lucide-react";
import { useState } from "react";

import { LOCALES, LOCALE_NAMES, useI18n } from "@/lib/i18n";

export function LanguageSwitcher({ direction = "up" }: { direction?: "up" | "down" }) {
  const { locale, setLocale, t } = useI18n();
  const [open, setOpen] = useState(false);
  const menuPos = direction === "up" ? "bottom-full mb-1" : "top-full mt-1";
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={t("shell.language")}
        className="flex items-center gap-1.5 min-h-11 rounded px-2 py-0.5 text-xs text-[var(--color-muted-foreground)] sm:min-h-0 sm:px-1.5 sm:text-[11px] transition-colors hover:bg-[var(--color-accent)]"
      >
        <GlobeIcon className="h-3.5 w-3.5" />
        {LOCALE_NAMES[locale]}
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <div className={`absolute left-0 z-30 max-h-72 w-40 overflow-y-auto rounded-md border border-[var(--color-border)] bg-[var(--color-card)] py-1 shadow-lg ${menuPos}`}>
            {LOCALES.map((l) => (
              <button
                key={l}
                onClick={() => { setLocale(l); setOpen(false); }}
                className={`flex w-full items-center justify-between px-2.5 py-1.5 text-left text-xs transition-colors hover:bg-[var(--color-accent)] ${
                  locale === l ? "font-medium text-[var(--color-brand)]" : ""
                }`}
              >
                {LOCALE_NAMES[l]}
                {locale === l && <CheckIcon className="h-3 w-3" />}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
