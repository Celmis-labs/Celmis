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
        "relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border transition-[background-color,border-color] duration-200 ease-out-quint",
        // The pill is 20x36 by design, which is a quarter of a fingertip. The
        // pseudo-element carries a 44px hit area without changing how it
        // looks or how it sits in a row — every call site would otherwise
        // have to remember this, and none of them did.
        "after:absolute after:-inset-x-1 after:-inset-y-3 after:content-[''] sm:after:hidden",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-background)]",
        "disabled:cursor-not-allowed disabled:opacity-50",
        // Off is the field-edge grey (3:1 against the card), not the old
        // secondary fill that was 1.08:1 — an off switch has to be seen to
        // be read as off rather than as missing.
        checked
          ? "border-[var(--color-primary)] bg-[var(--color-primary)]"
          : "border-[var(--color-input)] bg-[var(--color-input)]/45 hover:bg-[var(--color-input)]/65",
        className,
      )}
      {...props}
    >
      <span
        className={cn(
          "pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-[0_1px_2px_oklch(0_0_0/0.25)] transition-transform duration-200 ease-out-quint",
          checked ? "translate-x-4" : "translate-x-0",
        )}
      />
    </button>
  );
}
