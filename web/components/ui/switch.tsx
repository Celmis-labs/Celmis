"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

interface SwitchProps {
  checked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
  disabled?: boolean;
  className?: string;
  id?: string;
  "aria-label"?: string;
}

export function Switch({
  checked = false,
  onCheckedChange,
  disabled,
  className,
  id,
  ...props
}: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      id={id}
      onClick={() => onCheckedChange?.(!checked)}
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent transition-colors",
        // The pill is 20x36 by design, which is a quarter of a fingertip. The
        // pseudo-element carries a 44px hit area without changing how it
        // looks or how it sits in a row — every call site would otherwise
        // have to remember this, and none of them did.
        "after:absolute after:-inset-x-1 after:-inset-y-3 after:content-[''] sm:after:hidden",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]",
        "disabled:cursor-not-allowed disabled:opacity-50",
        checked
          ? "bg-[var(--color-brand)]"
          : "bg-[var(--color-secondary)]",
        className,
      )}
      {...props}
    >
      <span
        className={cn(
          "pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-sm transition-transform",
          checked ? "translate-x-[18px]" : "translate-x-0.5",
        )}
      />
    </button>
  );
}
