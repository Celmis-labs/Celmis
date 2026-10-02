"use client";

import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import * as m from "motion/react-m";

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

/**
 * A tooltip that escapes its container — for the sidebar rail.
 *
 * The CSS one above is an absolutely-positioned child, and the rail is a
 * 48px column with `overflow: hidden` (it has to clip the labels while its
 * width animates), so a bubble meant to hang to the right of an icon was cut
 * off at the column's edge. This one is measured from the trigger and painted
 * into <body>, like the workspace menu, so no ancestor can clip it.
 *
 * Shown on hover and on keyboard focus, never on a touch press: a tap is a
 * navigation, and a bubble that appears under the finger as the page changes
 * is noise. The trigger already carries the same words as its accessible
 * name, so the bubble is decoration for sighted pointer users.
 */
export function FloatingTooltip({
  label,
  shortcut,
  side = "right",
  disabled = false,
  children,
}: {
  label: string;
  /** Rendered as a <kbd> beside the label, e.g. "⌘B". */
  shortcut?: string;
  side?: "right" | "bottom";
  /** Render the child alone — e.g. while the label is visible anyway. */
  disabled?: boolean;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const [at, setAt] = useState<{ top: number; left: number } | null>(null);
  const show = (e: React.PointerEvent | React.FocusEvent) => {
    if ("pointerType" in e && e.pointerType === "touch") return;
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    setAt(side === "right"
      ? { top: r.top + r.height / 2, left: r.right + 8 }
      : { top: r.bottom + 6, left: r.left + r.width / 2 });
  };
  const hide = () => setAt(null);

  if (disabled) return <>{children}</>;
  return (
    <span
      ref={ref}
      className="inline-flex"
      onPointerEnter={show}
      onPointerLeave={hide}
      onFocus={show}
      onBlur={hide}
      onClick={hide}
    >
      {children}
      {at && createPortal(
        // Two boxes: the outer one is placed and centred by CSS, the inner
        // one is animated. One box doing both would have Motion's `x`/`y`
        // overwrite the centring translate on the first frame.
        <span
          style={{ position: "fixed", top: at.top, left: at.left }}
          className={cn(
            "pointer-events-none z-50",
            side === "right" ? "-translate-y-1/2" : "-translate-x-1/2",
          )}
        >
          <m.span
            role="tooltip"
            initial={{ opacity: 0, x: side === "right" ? -4 : 0, y: side === "bottom" ? -2 : 0 }}
            animate={{ opacity: 1, x: 0, y: 0 }}
            transition={{ duration: 0.14, ease: [0.2, 0, 0, 1] }}
            className="flex items-center gap-2 whitespace-nowrap rounded-md border border-[var(--color-border)] bg-[var(--color-popover)] px-2 py-1 text-xs font-medium text-[var(--color-popover-foreground)] shadow-[var(--shadow-md)]"
          >
            {label}
            {shortcut && (
              <kbd className="rounded border border-[var(--color-border)] bg-[var(--color-muted)] px-1 font-sans text-[10px] text-[var(--color-muted-foreground)]">
                {shortcut}
              </kbd>
            )}
          </m.span>
        </span>,
        document.body,
      )}
    </span>
  );
}
