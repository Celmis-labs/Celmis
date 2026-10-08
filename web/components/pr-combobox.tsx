"use client";

/**
 * Searchable picker for one open pull request.
 *
 * WHY. The review form used to hand every open PR to a native <select>. With a
 * few hundred PRs that is a list as tall as the screen, with titles wider than
 * the form and no way to find a PR by number or name.
 *
 * WHAT IT DOES.
 *   - Filters as you type (see lib/pr-search.ts): PR number with or without
 *     "#" (an exact number ranks first), title, source/target branch, author;
 *     case- and accent-insensitive.
 *   - The panel is as wide as the trigger (never wider than the viewport) and
 *     at most min(320px, 50vh) tall, scrolling inside; long titles are cut
 *     with an ellipsis and carry the full text in their `title` attribute.
 *   - Renders at most MAX_ROWS rows and says how many more matched.
 *   - `onSearch` gets the debounced term so a caller whose list is only a
 *     slice of all PRs can ask the server for the rest.
 *   - Keyboard: ↑/↓/Home/End move, Enter picks, Esc closes. ARIA combobox +
 *     listbox with aria-activedescendant.
 *   - Portalled and fixed-positioned, like BranchCombobox, so scrolling
 *     containers cannot clip it.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CheckIcon, ChevronDownIcon, GitPullRequestIcon, Loader2Icon, SearchIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";
import { filterPrs, type SearchablePr } from "@/lib/pr-search";

export type PrOption = SearchablePr & {
  /** What `onChange` receives for this row. */
  value: string;
};

/** Rows rendered at once; the rest is reached by refining the search. */
export const MAX_ROWS = 200;
const DEBOUNCE_MS = 250;
const GAP = 4;
const MARGIN = 8;

function branchLine(p: PrOption, unknown: string): string {
  const base = `${p.target_branch ?? unknown} ← ${p.source_branch ?? unknown}`;
  return p.author ? `${base} · ${p.author}` : base;
}

export function PrCombobox({
  value,
  onChange,
  options,
  selected,
  onSearch,
  searching = false,
  id,
  className,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  options: PrOption[];
  /** The chosen PR, for the trigger label when a search dropped it from `options`. */
  selected?: PrOption | null;
  /** Called with the debounced, trimmed term ("" = cleared). */
  onSearch?: (term: string) => void;
  /** A server search is in flight. */
  searching?: boolean;
  id?: string;
  className?: string;
  placeholder?: string;
  ariaLabel?: string;
}) {
  const t = useT();
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const [active, setActive] = useState(0);
  const [rect, setRect] = useState<{
    top?: number; bottom?: number; left: number; width: number; room: number;
  } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const onSearchRef = useRef(onSearch);
  useEffect(() => {
    onSearchRef.current = onSearch;
  });

  useEffect(() => {
    const handle = window.setTimeout(() => onSearchRef.current?.(term.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [term]);

  useEffect(() => {
    if (!open) return;
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect();
      if (!r) return;
      const vw = document.documentElement.clientWidth;
      const vh = window.innerHeight;
      const width = Math.min(r.width, vw - 2 * MARGIN);
      const left = Math.max(MARGIN, Math.min(r.left, vw - MARGIN - width));
      const below = vh - r.bottom - GAP - MARGIN;
      const above = r.top - GAP - MARGIN;
      // Open upward only when below is cramped and above is roomier.
      if (below < 200 && above > below) {
        setRect({ bottom: vh - r.top + GAP, left, width, room: above });
      } else {
        setRect({ top: r.bottom + GAP, left, width, room: below });
      }
    };
    const frame = window.requestAnimationFrame(place);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    const outside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
      setTerm("");
    };
    document.addEventListener("mousedown", outside);
    const focus = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(focus);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      document.removeEventListener("mousedown", outside);
    };
  }, [open]);

  const matches = useMemo(() => filterPrs(options, term), [options, term]);
  const rows = matches.length > MAX_ROWS ? matches.slice(0, MAX_ROWS) : matches;
  const hidden = matches.length - rows.length;
  const activeIndex = Math.min(active, Math.max(0, rows.length - 1));
  const current = options.find((o) => o.value === value) ?? (selected?.value === value ? selected : undefined);
  const unknown = t("prPicker.unknownBranch");

  useEffect(() => {
    if (!open) return;
    listRef.current?.children[activeIndex]?.scrollIntoView({ block: "nearest" });
  }, [open, activeIndex, rows]);

  const close = () => {
    setOpen(false);
    setTerm("");
    setActive(0);
    triggerRef.current?.focus();
  };
  const pick = (v: string) => {
    onChange(v);
    close();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive(Math.min(activeIndex + 1, rows.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(Math.max(activeIndex - 1, 0));
    } else if (e.key === "Home") {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End") {
      e.preventDefault();
      setActive(Math.max(rows.length - 1, 0));
    } else if (e.key === "Enter") {
      // Never let Enter submit the surrounding form from inside the picker.
      e.preventDefault();
      const row = rows[activeIndex];
      if (row) pick(row.value);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "Tab") {
      setOpen(false);
      setTerm("");
    }
  };

  const panel = open && rect && (
    <div
      ref={panelRef}
      style={{
        top: rect.top, bottom: rect.bottom, left: rect.left, width: rect.width,
        maxHeight: rect.room,
      }}
      className="fixed z-50 flex flex-col overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-popover,var(--color-background))] p-1 shadow-[var(--shadow-md,0_4px_12px_rgb(0_0_0/0.12))]"
    >
      <div className="flex items-center gap-2 border-b border-[var(--color-border)] px-2 pb-1">
        <SearchIcon className="h-3.5 w-3.5 shrink-0 opacity-60" />
        <input
          ref={inputRef}
          role="combobox"
          aria-expanded
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={rows.length ? `${listId}-${activeIndex}` : undefined}
          aria-label={ariaLabel ?? t("prPicker.label")}
          value={term}
          onChange={(e) => { setTerm(e.target.value); setActive(0); }}
          onKeyDown={onKeyDown}
          placeholder={t("prPicker.searchPlaceholder")}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          // 16px on a phone: smaller makes iOS zoom the page on focus.
          className="h-9 w-full min-w-0 bg-transparent text-base outline-none placeholder:text-[var(--color-muted-foreground)] sm:h-8 sm:text-sm"
        />
        {searching && <Loader2Icon className="h-3.5 w-3.5 shrink-0 animate-spin opacity-60" />}
      </div>
      <ul
        ref={listRef}
        id={listId}
        role="listbox"
        aria-label={ariaLabel ?? t("prPicker.label")}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain py-1"
        style={{ maxHeight: "min(320px, 50vh)" }}
      >
        {rows.map((o, i) => {
          const line = branchLine(o, unknown);
          const full = `#${o.number} — ${o.title}\n${line}`;
          return (
            <li
              key={o.value}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={o.value === value}
              title={full}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(o.value)}
              className={cn(
                "flex cursor-pointer items-center justify-between gap-2 rounded px-2 py-1.5 text-sm",
                i === activeIndex && "bg-[var(--color-accent)]",
              )}
            >
              <span className="flex min-w-0 flex-col">
                <span className="flex min-w-0 items-baseline gap-1.5">
                  <span className="shrink-0 font-mono text-xs text-[var(--color-muted-foreground)]">
                    #{o.number}
                  </span>
                  <span className="truncate">{o.title}</span>
                </span>
                <span className="truncate text-xs text-[var(--color-muted-foreground)]">{line}</span>
              </span>
              {o.value === value && (
                <CheckIcon className="h-3.5 w-3.5 shrink-0 text-[var(--color-brand)]" />
              )}
            </li>
          );
        })}
      </ul>
      <div className="space-y-0.5 px-2 pt-1 text-xs text-[var(--color-muted-foreground)]" aria-live="polite">
        {matches.length === 0 ? (
          <p>{t("prPicker.noMatches")}</p>
        ) : (
          <p>{t("prPicker.count", { count: matches.length })}</p>
        )}
        {hidden > 0 && <p>{t("prPicker.more", { count: hidden })}</p>}
      </div>
    </div>
  );

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        id={id}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        title={current ? `#${current.number} — ${current.title}` : undefined}
        onClick={() => (open ? close() : setOpen(true))}
        onKeyDown={(e) => {
          if (!open && (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            setOpen(true);
          }
        }}
        className={cn(
          "flex h-11 w-full min-w-0 items-center justify-between gap-2 rounded-md border border-[var(--color-border)] bg-transparent px-3 text-base sm:h-9 sm:text-sm transition-colors hover:bg-[var(--color-accent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand)]",
          className,
        )}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <GitPullRequestIcon className="h-3.5 w-3.5 shrink-0 opacity-60" />
          <span className={cn("truncate", !current && "text-[var(--color-muted-foreground)]")}>
            {current ? `#${current.number} — ${current.title}` : (placeholder ?? t("prPicker.placeholder"))}
          </span>
        </span>
        <ChevronDownIcon className="h-3.5 w-3.5 shrink-0 opacity-60" />
      </button>
      {panel && typeof document !== "undefined" && createPortal(panel, document.body)}
    </>
  );
}
