"use client";

/**
 * A checklist with a tri-state "Select all", a counter ("N of M selected") and, when
 * the list is long, a filter. "Select all" acts on the rows the filter leaves visible
 * (lib/pickers.ts); rows the filter hides keep their ticks.
 */

import { useEffect, useId, useRef, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { filterOptions, selectAllState, toggleAllVisible } from "@/lib/pickers";

/** A filter is worth its row from this many items. */
export const FILTER_FROM = 8;

export function CheckList<T>({
  items, getId, searchText, picked, onChange, canAdd, renderItem,
  selectAllLabel, counterLabel, filterPlaceholder, noMatchText, className, maxHeightClass = "max-h-44",
}: {
  items: readonly T[];
  getId: (item: T) => string;
  /** What the filter matches on. */
  searchText: (item: T) => string;
  picked: readonly string[];
  onChange: (next: string[]) => void;
  /** May this unticked row be ticked now? (a full share, a per-token cap). */
  canAdd?: (item: T, picked: readonly string[]) => boolean;
  renderItem: (item: T) => ReactNode;
  selectAllLabel: string;
  /** The counter, already formatted: "N of M selected". */
  counterLabel: (ticked: number, total: number) => string;
  filterPlaceholder: string;
  noMatchText: string;
  className?: string;
  maxHeightClass?: string;
}) {
  const [filter, setFilter] = useState("");
  const boxRef = useRef<HTMLInputElement>(null);
  const uid = useId();

  const rows = filterOptions(
    items.map((it) => ({ value: getId(it), label: searchText(it), item: it })), filter,
  ).map((r) => r.item);
  const visible = rows.map(getId);
  const byId = new Map(items.map((it) => [getId(it), it]));
  const addable = (id: string, cur: readonly string[]) => {
    const it = byId.get(id);
    return !it || !canAdd || canAdd(it, cur);
  };
  const all = selectAllState(visible, picked, addable);
  const everTicked = picked.filter((id) => byId.has(id)).length;

  useEffect(() => {
    if (boxRef.current) boxRef.current.indeterminate = all.state === "some";
  }, [all.state]);

  if (items.length === 0) return null;

  return (
    <div className={cn("space-y-1.5", className)}>
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
        <label className="flex cursor-pointer items-center gap-2 text-sm">
          <Checkbox
            ref={boxRef}
            checked={all.state === "all"}
            aria-checked={all.state === "some" ? "mixed" : all.state === "all"}
            disabled={all.total === 0}
            onChange={() => onChange(toggleAllVisible(visible, picked, addable))}
          />
          <span>{selectAllLabel}</span>
        </label>
        <span className="text-xs text-[var(--color-muted-foreground)]" aria-live="polite">
          {counterLabel(everTicked, items.length)}
        </span>
      </div>
      {items.length >= FILTER_FROM && (
        <Input
          id={`${uid}-filter`}
          type="search"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder={filterPlaceholder}
          aria-label={filterPlaceholder}
          className="h-9"
        />
      )}
      <div className={cn("space-y-1 overflow-y-auto rounded-md border border-[var(--color-border)] p-2", maxHeightClass)}>
        {rows.length === 0 && (
          <div className="text-xs text-[var(--color-muted-foreground)]">{noMatchText}</div>
        )}
        {rows.map((it) => (
          <div key={getId(it)}>{renderItem(it)}</div>
        ))}
      </div>
    </div>
  );
}
