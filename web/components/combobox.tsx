"use client";

/**
 * One text field that is also the list: type, and the matches drop down right under
 * it. Arrow keys move, Enter picks, Escape closes; a click picks too. Once something
 * is chosen the field shows it and a button clears it to pick another.
 *
 * Built to ARIA 1.2: the input is the `combobox`, the panel a `listbox` of `option`s,
 * and the highlighted row is named by `aria-activedescendant` (focus never leaves the
 * input). Matching is local (lib/pickers.ts) unless `filterLocally` is off, for a list
 * the server already searched; then `onQueryChange` carries what to ask for.
 */

import { useEffect, useId, useRef, useState } from "react";
import { CheckIcon, ChevronDownIcon, Loader2Icon, XIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { comboStep, filterOptions, type PickerOption } from "@/lib/pickers";

/** Rows drawn at once; the rest is reached by typing more. */
const MAX_ROWS = 100;

export function Combobox({
  options, selected, onSelect, onQueryChange, filterLocally = true, loading = false,
  format = (o) => o.label, id, className, placeholder, ariaLabel, emptyText, clearLabel,
  moreText, defaultOpen = false,
}: {
  options: readonly PickerOption[];
  /** The chosen option, kept by the caller so a new search cannot lose it. */
  selected: PickerOption | null;
  onSelect: (option: PickerOption | null) => void;
  /** Every change of what is typed ("" once it is cleared or an option is picked). */
  onQueryChange?: (query: string) => void;
  filterLocally?: boolean;
  loading?: boolean;
  /** The text the field shows for the chosen option. */
  format?: (option: PickerOption) => string;
  id?: string;
  className?: string;
  placeholder?: string;
  ariaLabel?: string;
  emptyText: string;
  clearLabel: string;
  /** "N more, keep typing", given the number not drawn. */
  moreText?: (hidden: number) => string;
  defaultOpen?: boolean;
}) {
  const listId = useId();
  const [open, setOpen] = useState(defaultOpen);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const matches = filterLocally ? filterOptions(options, editing ? query : "") : [...options];
  const rows = matches.slice(0, MAX_ROWS);
  const current = Math.min(active, Math.max(rows.length - 1, 0));
  const activeId = open && rows.length ? `${listId}-${current}` : undefined;

  useEffect(() => {
    if (!open) return;
    listRef.current?.querySelector<HTMLElement>(`[id="${listId}-${current}"]`)
      ?.scrollIntoView?.({ block: "nearest" });
  }, [open, current, listId]);

  const type = (value: string) => {
    if (selected) onSelect(null);
    setEditing(true);
    setQuery(value);
    setActive(0);
    setOpen(true);
    onQueryChange?.(value);
  };
  const pick = (o: PickerOption) => {
    onSelect(o);
    setEditing(false);
    setQuery("");
    setOpen(false);
    onQueryChange?.("");
  };
  const clear = () => {
    onSelect(null);
    setEditing(true);
    setQuery("");
    setActive(0);
    setOpen(true);
    onQueryChange?.("");
    inputRef.current?.focus();
  };

  const shown = editing ? query : selected ? format(selected) : "";

  return (
    <div className={cn("relative", className)}>
      <input
        ref={inputRef}
        id={id}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={activeId}
        aria-label={ariaLabel}
        autoComplete="off"
        spellCheck={false}
        value={shown}
        placeholder={placeholder}
        onChange={(e) => type(e.target.value)}
        onFocus={(e) => { if (selected && !editing) e.target.select(); }}
        onClick={() => setOpen(true)}
        onBlur={() => { setOpen(false); setEditing(false); setQuery(""); }}
        onKeyDown={(e) => {
          const step = comboStep({ open, active: current }, e.key, rows.length);
          if (step.handled) e.preventDefault();
          setOpen(step.open);
          setActive(step.active);
          if (step.pick && rows[step.active]) pick(rows[step.active]);
        }}
        className={cn(
          "flex h-11 w-full rounded-lg border border-[var(--color-input)] bg-[var(--color-card)] py-1 pl-3 pr-16 text-base shadow-[var(--shadow-xs)] sm:h-9 sm:text-sm",
          "transition-[color,background-color,border-color,box-shadow] duration-150 placeholder:text-[var(--color-subtle-foreground)]",
          "hover:border-[var(--color-muted-foreground)] focus-visible:border-[var(--color-ring)] focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-[var(--color-ring)]/25",
        )}
      />
      <div className="absolute inset-y-0 right-1 flex items-center gap-0.5">
        {loading && <Loader2Icon aria-hidden className="h-3.5 w-3.5 animate-spin text-[var(--color-muted-foreground)]" />}
        {selected && (
          <button
            type="button"
            aria-label={clearLabel}
            title={clearLabel}
            onMouseDown={(e) => e.preventDefault()}
            onClick={clear}
            className="grid size-7 place-items-center rounded-md text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]"
          >
            <XIcon aria-hidden className="h-3.5 w-3.5" />
          </button>
        )}
        <ChevronDownIcon aria-hidden className="mr-1 h-3.5 w-3.5 text-[var(--color-muted-foreground)]" />
      </div>
      {open && (
        <div className="absolute left-0 right-0 top-full z-50 mt-1 overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] shadow-lg">
          {rows.length > 0 && (
            <ul
              ref={listRef}
              id={listId}
              role="listbox"
              aria-label={ariaLabel}
              className="max-h-64 overflow-y-auto p-1"
            >
              {rows.map((o, i) => (
                <li
                  key={o.value}
                  id={`${listId}-${i}`}
                  role="option"
                  aria-selected={selected?.value === o.value}
                  data-active={i === current ? "true" : undefined}
                  // mousedown, not click: the input's blur would close the list first.
                  onMouseDown={(e) => { e.preventDefault(); pick(o); }}
                  onMouseMove={() => setActive(i)}
                  className={cn(
                    "flex cursor-pointer items-baseline justify-between gap-3 rounded-md px-2 py-1.5 text-sm",
                    i === current && "bg-[var(--color-accent)]",
                  )}
                >
                  <span className="flex min-w-0 items-baseline gap-2">
                    <span className="truncate">{o.label}</span>
                    {o.hint && (
                      <span className="truncate text-xs text-[var(--color-muted-foreground)]">{o.hint}</span>
                    )}
                  </span>
                  {selected?.value === o.value && (
                    <CheckIcon aria-hidden className="h-3.5 w-3.5 shrink-0 text-[var(--color-primary)]" />
                  )}
                </li>
              ))}
            </ul>
          )}
          {rows.length === 0 && (
            // The listbox is only rendered with rows, so the empty state is plain text.
            <div id={listId} role="status" aria-live="polite"
                 className="px-3 py-2 text-sm text-[var(--color-muted-foreground)]">
              {loading ? "…" : emptyText}
            </div>
          )}
          {matches.length > rows.length && moreText && (
            <div className="border-t border-[var(--color-border)] px-3 py-1.5 text-xs text-[var(--color-muted-foreground)]">
              {moreText(matches.length - rows.length)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
