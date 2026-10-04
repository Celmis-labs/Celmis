"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

export type Segment<V extends string> = {
  value: V;
  label: React.ReactNode;
  /** A number shown after the label (items in that tab, …). */
  count?: number;
};

/**
 * A row of mutually exclusive choices with a sliding thumb: a list filter
 * (All / Open / Fixed), a time window (7d / 30d / 90d).
 *
 * `semantics` picks the ARIA pattern: "tabs" when the choice swaps the list
 * below it, "radio" when it sets a value. Either way the keyboard model is
 * the roving one — Tab lands on the chosen segment, arrows / Home / End
 * move and choose.
 *
 * The thumb is positioned by writing to its style in a layout effect, not
 * through state, so a choice costs one render and the thumb glides with a
 * CSS transform (no layout animation, no Motion feature bundle).
 */
export function SegmentedControl<V extends string>({
  value,
  onValueChange,
  segments,
  label,
  semantics = "radio",
  size = "default",
  className,
}: {
  value: V;
  onValueChange: (v: V) => void;
  segments: Segment<V>[];
  label: string;
  semantics?: "radio" | "tabs";
  size?: "sm" | "default";
  className?: string;
}) {
  const listRef = React.useRef<HTMLDivElement>(null);
  const thumbRef = React.useRef<HTMLSpanElement>(null);
  const placed = React.useRef(false);

  React.useLayoutEffect(() => {
    const list = listRef.current;
    const thumb = thumbRef.current;
    if (!list || !thumb) return;
    const place = () => {
      const active = list.querySelector<HTMLElement>('[data-active="true"]');
      if (!active) {
        thumb.style.opacity = "0";
        return;
      }
      // No glide on the first placement: the thumb should start where it is.
      if (!placed.current) thumb.style.transition = "none";
      thumb.style.opacity = "1";
      thumb.style.width = `${active.offsetWidth}px`;
      thumb.style.transform = `translateX(${active.offsetLeft}px)`;
      if (!placed.current) {
        void thumb.offsetWidth;
        thumb.style.transition = "";
        placed.current = true;
      }
    };
    place();
    // Labels change width when counts arrive or the language changes.
    const ro = new ResizeObserver(place);
    ro.observe(list);
    return () => ro.disconnect();
  }, [value, segments]);

  const move = (from: number, delta: number | "first" | "last") => {
    const n = segments.length;
    const to = delta === "first" ? 0 : delta === "last" ? n - 1 : (from + delta + n) % n;
    onValueChange(segments[to].value);
    listRef.current?.querySelectorAll<HTMLButtonElement>("button")[to]?.focus();
  };

  const isTabs = semantics === "tabs";
  return (
    <div
      ref={listRef}
      role={isTabs ? "tablist" : "radiogroup"}
      aria-label={label}
      className={cn(
        "relative inline-flex max-w-full items-center gap-0.5 overflow-x-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)] p-0.5 [scrollbar-width:none]",
        className,
      )}
    >
      <span
        ref={thumbRef}
        aria-hidden
        className="pointer-events-none absolute left-0 top-0.5 bottom-0.5 rounded-md bg-[var(--color-card)] opacity-0 shadow-[var(--shadow-sm)] ring-1 ring-[var(--color-border-strong)]/60 transition-[transform,width] duration-250 ease-out-quint motion-reduce:transition-none dark:bg-[var(--color-selected)]"
      />
      {segments.map((s, i) => {
        const active = s.value === value;
        return (
          <button
            key={s.value}
            type="button"
            role={isTabs ? "tab" : "radio"}
            aria-selected={isTabs ? active : undefined}
            aria-checked={isTabs ? undefined : active}
            tabIndex={active ? 0 : -1}
            data-active={active}
            onClick={() => onValueChange(s.value)}
            onKeyDown={(e) => {
              const k = e.key;
              if (k === "ArrowRight" || k === "ArrowDown") { e.preventDefault(); move(i, 1); }
              else if (k === "ArrowLeft" || k === "ArrowUp") { e.preventDefault(); move(i, -1); }
              else if (k === "Home") { e.preventDefault(); move(i, "first"); }
              else if (k === "End") { e.preventDefault(); move(i, "last"); }
            }}
            className={cn(
              "relative z-10 inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md font-medium transition-colors duration-150",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]",
              size === "sm" ? "h-9 px-2.5 text-xs sm:h-7" : "h-10 px-3 text-sm sm:h-8",
              active
                ? "text-[var(--color-foreground)]"
                : "text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]",
            )}
          >
            {s.label}
            {s.count !== undefined && (
              <span
                className={cn(
                  "rounded px-1 text-[11px] tabular-nums leading-4 transition-colors",
                  active
                    ? "bg-[var(--color-primary-soft)] text-[var(--color-primary-soft-foreground)]"
                    : "bg-[var(--color-neutral-soft)] text-[var(--color-muted-foreground)]",
                )}
              >
                {s.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
