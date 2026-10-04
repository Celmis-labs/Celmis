"use client";
import * as React from "react";
import { CheckIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * A native checkbox in the app's clothes. Still an <input type="checkbox">,
 * so labels, forms, Space and screen readers behave exactly as before; only
 * the box is drawn by us (the OS box ignored the theme and was 13px).
 * The wrapper is 16px; a parent <label> carries the larger hit area.
 */
export const Checkbox = React.forwardRef<
  HTMLInputElement,
  Omit<React.InputHTMLAttributes<HTMLInputElement>, "type">
>(({ className, ...props }, ref) => (
  <span className={cn("relative inline-grid size-4 shrink-0 place-items-center align-middle", className)}>
    <input
      ref={ref}
      type="checkbox"
      className={cn(
        "peer col-start-1 row-start-1 size-4 cursor-pointer appearance-none rounded-[5px] border border-[var(--color-input)] bg-[var(--color-card)]",
        "transition-[background-color,border-color,box-shadow] duration-150",
        "hover:border-[var(--color-muted-foreground)]",
        "checked:border-[var(--color-primary)] checked:bg-[var(--color-primary)]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-background)]",
        "disabled:cursor-not-allowed disabled:opacity-50",
      )}
      {...props}
    />
    <CheckIcon
      aria-hidden
      strokeWidth={3}
      className="pointer-events-none col-start-1 row-start-1 size-3 scale-50 text-[var(--color-primary-foreground)] opacity-0 transition-[opacity,transform] duration-150 ease-out-quint peer-checked:scale-100 peer-checked:opacity-100"
    />
  </span>
));
Checkbox.displayName = "Checkbox";
