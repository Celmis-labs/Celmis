"use client";

import { cn } from "@/lib/utils";

/**
 * Lightweight CSS-only tooltip — no Radix, no portal.
 *
 * Renders a wrapping <span aria-label> and shows the label on hover AND
 * keyboard focus (group-hover + group-focus-within), so it stays accessible
 * for keyboard users as long as the child is focusable.
 */
export function Tooltip({
  label,
  side = "top",
  className,
  children,
}: {
  label: string;
  /** Placement of the bubble relative to the child. */
  side?: "top" | "right" | "bottom";
  className?: string;
  children: React.ReactNode;
}) {
  const placement = {
    top: "bottom-full left-1/2 mb-1.5 -translate-x-1/2",
    bottom: "top-full left-1/2 mt-1.5 -translate-x-1/2",
    right: "left-full top-1/2 ml-1.5 -translate-y-1/2",
  }[side];
  return (
    <span aria-label={label} className={cn("group/tip relative inline-flex", className)}>
      {children}
      <span
        role="tooltip"
        className={cn(
          // A nowrap absolute box still counts toward scrollWidth, so a long
          // tooltip made the whole page scroll sideways on a phone — measured
          // at 512px against a 390px viewport on /dependencies. Wrap instead
          // of overflowing, and never exceed the viewport.
          "pointer-events-none absolute z-50 max-w-[calc(100vw-2rem)] text-balance rounded-md border border-[var(--color-border)] sm:whitespace-nowrap",
          "bg-[var(--color-popover)] px-2 py-1 text-[11px] text-[var(--color-popover-foreground)]",
          "opacity-0 shadow-[var(--shadow-md)] transition-opacity duration-100",
          "group-hover/tip:opacity-100 group-focus-within/tip:opacity-100",
          placement,
        )}
      >
        {label}
      </span>
    </span>
  );
}
