"use client";

/**
 * One searchable branch picker for every page that asks for a branch.
 *
 * WHY. Each page used to fetch GET …/branches once and hand the answer to a
 * plain <Select>. The server read ONE provider page (100 names) and the
 * dropdown showed it as the whole list: on a repository with a few hundred
 * branches, the branch you wanted was simply missing, with no search and no
 * hint that anything had been cut.
 *
 * WHAT IT DOES.
 *   - Fetches only when opened (a provider call per repository is not free),
 *     then again per search term, debounced — the server filters its cached
 *     full listing, so a search finds branches far past the first page.
 *   - Says what it is not showing: "N more — refine the search" when the
 *     matches outnumber the rows, and a note when the provider listing itself
 *     was cut at the server's cap.
 *   - Keyboard: ↑/↓ move, Enter picks, Esc closes, Home/End jump. ARIA
 *     combobox + listbox, so a screen reader hears the active option.
 *   - `allowCustom` adds "Use “…”" for a name not in the list — only on the
 *     pages that already let you type a branch by hand.
 *   - The panel is portalled and fixed-positioned: several pickers sit inside
 *     scrolling containers that would clip an absolutely positioned list.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { CheckIcon, ChevronDownIcon, GitBranchIcon, Loader2Icon, SearchIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";

export type BranchOption = {
  value: string;
  /** Defaults to `value`. */
  label?: string;
  /** Muted text after the label — a count, "default", anything secondary. */
  hint?: string;
};

export type BranchSearchResult = {
  options: BranchOption[];
  /** Every match the server knows of — may exceed `options.length`. */
  total: number;
  /** The provider's listing was cut at the server cap. */
  truncated: boolean;
  defaultBranch?: string | null;
  /** Why nothing came back, when nothing did. */
  error?: string | null;
};

/** How long typing has to pause before the server is asked again. */
const DEBOUNCE_MS = 250;

export function BranchCombobox({
  value,
  onChange,
  search,
  queryKey,
  leadingOptions = [],
  allowCustom = false,
  selected,
  keepOpenOnSelect = false,
  placeholder,
  disabled = false,
  id,
  className,
  defaultOpen = false,
  onClose,
  ariaLabel,
}: {
  /** Current value; "" is a legitimate value when a leading option uses it. */
  value: string;
  onChange: (value: string) => void;
  /** Server search: called with the debounced, trimmed term ("" = no filter). */
  search: (q: string) => Promise<BranchSearchResult>;
  /** react-query key prefix; the term is appended. */
  queryKey: readonly unknown[];
  /** Fixed rows above the branches — "provider default", "all branches"… */
  leadingOptions?: BranchOption[];
  /** Offer the typed text itself as a choice. */
  allowCustom?: boolean;
  /** Multi-pick: every value shown with a check mark. Defaults to [value]. */
  selected?: string[];
  keepOpenOnSelect?: boolean;
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  className?: string;
  /** Mount already open (an inline editor that the user just asked for). */
  defaultOpen?: boolean;
  onClose?: () => void;
  ariaLabel?: string;
}) {
  const t = useT();
  const listId = useId();
  const [open, setOpen] = useState(defaultOpen);
  const [term, setTerm] = useState("");
  const [debounced, setDebounced] = useState("");
  const [active, setActive] = useState(0);
  const [rect, setRect] = useState<{ top: number; left: number; width: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // Callers pass `onClose` inline; reading it through a ref keeps the
  // listeners below from being torn down and re-added on every render.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(term.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [term]);

  const result = useQuery({
    queryKey: [...queryKey, debounced],
    queryFn: () => search(debounced),
    enabled: open && !disabled,
    staleTime: 60_000,
    retry: false,
    placeholderData: (prev) => prev,
  });

  // Where the panel goes. Measured on open and whenever anything scrolls or
  // resizes — all from event callbacks, never during render.
  useEffect(() => {
    if (!open) return;
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect();
      if (r) setRect({ top: r.bottom + 4, left: r.left, width: r.width });
    };
    const frame = window.requestAnimationFrame(place);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    const outside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
      setTerm("");
      onCloseRef.current?.();
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

  const needle = term.trim();
  const rows = useMemo(() => {
    const low = needle.toLowerCase();
    const lead = leadingOptions.filter((o) =>
      !low || (o.label ?? o.value).toLowerCase().includes(low));
    const fromServer = (result.data?.options ?? []).filter(
      (o) => !lead.some((l) => l.value === o.value));
    const out: Array<BranchOption & { custom?: boolean }> = [];
    const known = [...lead, ...fromServer].some((o) => o.value === needle);
    if (allowCustom && needle && !known) out.push({ value: needle, custom: true });
    return [...out, ...lead, ...fromServer] as Array<BranchOption & { custom?: boolean }>;
  }, [leadingOptions, result.data, allowCustom, needle]);

  const checked = new Set(selected ?? [value]);
  const current =
    leadingOptions.find((o) => o.value === value)?.label ?? (value || undefined);
  const shown = result.data?.options.length ?? 0;
  const more = Math.max(0, (result.data?.total ?? 0) - shown);
  const activeIndex = Math.min(active, Math.max(0, rows.length - 1));

  const close = () => {
    setOpen(false);
    setTerm("");
    setActive(0);
    onClose?.();
    triggerRef.current?.focus();
  };

  const pick = (v: string) => {
    onChange(v);
    if (keepOpenOnSelect) {
      setTerm("");
      inputRef.current?.focus();
    } else {
      close();
    }
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
      e.preventDefault();
      const row = rows[activeIndex];
      if (row) pick(row.value);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "Tab") {
      setOpen(false);
      onClose?.();
    }
  };

  const panel = open && rect && (
    <div
      ref={panelRef}
      style={{ top: rect.top, left: rect.left, minWidth: Math.max(rect.width, 240) }}
      className="fixed z-50 w-max max-w-[min(32rem,calc(100vw-1rem))] rounded-md border border-[var(--color-border)] bg-[var(--color-popover,var(--color-background))] p-1 shadow-[var(--shadow-md,0_4px_12px_rgb(0_0_0/0.12))]"
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
          aria-label={ariaLabel ?? t("branchPicker.label")}
          value={term}
          onChange={(e) => { setTerm(e.target.value); setActive(0); }}
          onKeyDown={onKeyDown}
          placeholder={t("branchPicker.searchPlaceholder")}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          className="h-8 w-full bg-transparent text-sm outline-none placeholder:text-[var(--color-muted-foreground)]"
        />
        {result.isFetching && <Loader2Icon className="h-3.5 w-3.5 shrink-0 animate-spin opacity-60" />}
      </div>
      <ul id={listId} role="listbox" className="max-h-72 overflow-y-auto py-1"
        aria-busy={result.isFetching}>
        {rows.map((o, i) => (
          <li
            key={`${o.custom ? "custom:" : ""}${o.value}`}
            id={`${listId}-${i}`}
            role="option"
            aria-selected={i === activeIndex}
            onMouseEnter={() => setActive(i)}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => pick(o.value)}
            className={cn(
              "flex cursor-pointer items-center justify-between gap-4 rounded px-2 py-1.5 text-sm",
              i === activeIndex && "bg-[var(--color-accent)]",
            )}
          >
            <span className="flex min-w-0 items-baseline gap-2">
              <span className={cn("truncate", !o.custom && o.label === undefined && "font-mono")}>
                {o.custom ? t("branchPicker.useCustom", { name: o.value }) : (o.label ?? o.value)}
              </span>
              {(o.hint || o.value === result.data?.defaultBranch) && !o.custom && (
                <span className="shrink-0 text-xs text-[var(--color-muted-foreground)]">
                  {o.hint ?? t("branchPicker.defaultMark")}
                </span>
              )}
            </span>
            {!o.custom && checked.has(o.value) && (
              <CheckIcon className="h-3.5 w-3.5 shrink-0 text-[var(--color-brand)]" />
            )}
          </li>
        ))}
      </ul>
      <div className="space-y-0.5 px-2 pb-1 text-xs text-[var(--color-muted-foreground)]" aria-live="polite">
        {result.isLoading && <p>{t("branchPicker.loading")}</p>}
        {result.isError && <p className="text-[var(--color-destructive)]">{t("branchPicker.error")}</p>}
        {result.data?.error === "no_credential" && <p>{t("branchPicker.noCredential")}</p>}
        {result.data?.error === "provider_error" && (
          <p className="text-[var(--color-destructive)]">{t("branchPicker.error")}</p>
        )}
        {result.data && !result.data.error && shown === 0 && needle && (
          <p>{t("branchPicker.noMatches", { q: needle })}</p>
        )}
        {more > 0 && <p>{t("branchPicker.more", { count: more })}</p>}
        {result.data?.truncated && <p>{t("branchPicker.truncated")}</p>}
      </div>
    </div>
  );

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        id={id}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => (open ? close() : setOpen(true))}
        onKeyDown={(e) => {
          if (!open && (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            setOpen(true);
          }
        }}
        className={cn(
          "flex h-11 items-center justify-between gap-2 rounded-md border border-[var(--color-border)] bg-transparent px-3 text-base sm:h-9 sm:text-sm transition-colors hover:bg-[var(--color-accent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand)] disabled:cursor-not-allowed disabled:opacity-50",
          className,
        )}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <GitBranchIcon className="h-3.5 w-3.5 shrink-0 opacity-60" />
          <span className={cn("truncate", !current && "text-[var(--color-muted-foreground)]")}>
            {current ?? placeholder ?? t("branchPicker.placeholder")}
          </span>
        </span>
        <ChevronDownIcon className="h-3.5 w-3.5 shrink-0 opacity-60" />
      </button>
      {panel && typeof document !== "undefined" && createPortal(panel, document.body)}
    </>
  );
}

/** Adapts a GET …/branches answer to what the combobox renders. */
export function toBranchResult(data: {
  branches: string[];
  total: number;
  truncated: boolean;
  default_branch: string | null;
  error?: string | null;
}): BranchSearchResult {
  return {
    options: data.branches.map((b) => ({ value: b })),
    total: data.total,
    truncated: data.truncated,
    defaultBranch: data.default_branch,
    error: data.error ?? null,
  };
}
