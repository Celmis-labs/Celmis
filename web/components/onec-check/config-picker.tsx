"use client";

/**
 * One field to find and pick the configuration: typing searches, the matches
 * open right under it, and the chosen one is what the field shows when closed.
 * A plain listbox combobox (WAI-ARIA 1.2): arrows move, Enter picks, Esc closes.
 */

import { useEffect, useId, useRef, useState } from "react";
import { CheckIcon, ChevronDownIcon, Loader2Icon, Share2Icon } from "lucide-react";

import type { OnecConfig } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** The index's compact age (`now`, `25m`, `3h`, `14d`) in the reader's words. */
export function ageWords(age: string, locale: string): string {
  const fmt = (n: number, unit: Intl.RelativeTimeFormatUnit) => {
    try {
      return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(n, unit);
    } catch {
      return new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(n, unit);
    }
  };
  if (age === "now") return fmt(0, "second");
  const m = /^(\d+)([mhd])$/.exec(age ?? "");
  if (!m) return "";
  const unit = ({ m: "minute", h: "hour", d: "day" } as const)[m[2] as "m" | "h" | "d"];
  return fmt(-Number(m[1]), unit);
}

type Row = { value: string; config: OnecConfig | null };

export function ConfigPicker({
  id, value, selected, onPick, query, onQuery, options, loading,
}: {
  id?: string;
  value: string;
  selected: OnecConfig | null;
  onPick: (c: OnecConfig | null) => void;
  query: string;
  onQuery: (q: string) => void;
  options: OnecConfig[];
  loading: boolean;
}) {
  const { t, locale } = useI18n();
  const listId = useId();
  const box = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);

  const rows: Row[] = [{ value: "", config: null }, ...options.map((c) => ({ value: c.slug, config: c }))];

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const pick = (row: Row) => {
    onPick(row.config);
    onQuery("");
    setOpen(false);
    field.current?.blur();
  };
  const show = () => {
    if (open) return;
    setOpen(true);
    const at = rows.findIndex((r) => r.value === value);
    setActive(Math.max(0, at));
  };
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) return show();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (i + step + rows.length) % rows.length);
    } else if (e.key === "Enter" && open) {
      e.preventDefault();
      const row = rows[Math.min(active, rows.length - 1)];
      if (row) pick(row);
    } else if (e.key === "Escape" && open) {
      e.preventDefault();
      onQuery("");
      setOpen(false);
    }
  };

  const closedText = selected ? selected.name : t("onecCheck.configNone");
  const meta = (c: OnecConfig) =>
    [c.ref, ageWords(c.age, locale)].filter(Boolean).join(" · ");

  return (
    <div ref={box} className="relative">
      <input
        ref={field}
        id={id}
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open ? `${listId}-${active}` : undefined}
        autoComplete="off"
        spellCheck={false}
        value={open ? query : closedText}
        placeholder={open ? t("onecCheck.configSearch") : undefined}
        onFocus={show}
        onClick={show}
        onChange={(e) => { onQuery(e.target.value); setActive(0); if (!open) setOpen(true); }}
        onKeyDown={onKey}
        data-testid="onec-config-picker"
        className={cn(
          "h-11 w-full rounded-lg border border-[var(--color-input)] bg-[var(--color-card)] pl-3 pr-9 text-base shadow-[var(--shadow-xs)] sm:h-9 sm:text-sm",
          "transition-[border-color,box-shadow] duration-150 hover:border-[var(--color-muted-foreground)] focus:outline-none focus-visible:border-[var(--color-ring)] focus-visible:ring-[3px] focus-visible:ring-[var(--color-ring)]/25",
          !open && !selected && "text-[var(--color-muted-foreground)]",
          open && "border-[var(--color-ring)]",
        )}
      />
      <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-[var(--color-muted-foreground)]">
        {loading && open
          ? <Loader2Icon className="h-3.5 w-3.5 animate-spin" />
          : <ChevronDownIcon className={cn("h-3.5 w-3.5 transition-transform duration-200", open && "rotate-180")} />}
      </span>
      {!open && selected && (
        <p className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-[var(--color-muted-foreground)]">
          <span className="font-mono">{meta(selected)}</span>
          {selected.shared_from && (
            <span className="inline-flex items-center gap-1">
              <Share2Icon className="h-3 w-3" />
              {t("onecCheck.sharedFrom", { owner: selected.shared_from })}
            </span>
          )}
        </p>
      )}
      {open && (
        <ul
          id={listId}
          role="listbox"
          aria-label={t("onecCheck.configLabel")}
          className="absolute z-30 mt-1 max-h-[50vh] w-full overflow-y-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-popover)] p-1 shadow-[var(--shadow-md)]"
        >
          {rows.map((row, i) => (
            <li
              key={row.value || "__none"}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={row.value === value}
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setActive(i)}
              onClick={() => pick(row)}
              className={cn(
                "flex cursor-pointer items-center justify-between gap-4 rounded-md px-2.5 py-2 text-sm",
                i === active && "bg-[var(--color-accent)]",
              )}
            >
              <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                <span className="truncate">{row.config ? row.config.name : t("onecCheck.configNone")}</span>
                {row.config && (
                  <span className="shrink-0 font-mono text-xs text-[var(--color-muted-foreground)]">
                    {meta(row.config)}
                  </span>
                )}
                {row.config?.shared_from && (
                  <span className="inline-flex shrink-0 items-center gap-1 text-xs text-[var(--color-muted-foreground)]">
                    <Share2Icon className="h-3 w-3" />
                    {t("onecCheck.sharedFrom", { owner: row.config.shared_from })}
                  </span>
                )}
              </span>
              {row.value === value && <CheckIcon className="h-3.5 w-3.5 shrink-0 text-[var(--color-primary)]" />}
            </li>
          ))}
          {!loading && options.length === 0 && query.trim() !== "" && (
            <li role="presentation" className="px-2.5 py-2 text-xs text-[var(--color-muted-foreground)]">
              {t("onecCheck.configNotFound")}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
