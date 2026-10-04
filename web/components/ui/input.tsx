"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, type, ...props }, ref) => (
  <input
    ref={ref}
    type={type}
    className={cn(
      // The page's background colour rather than transparent: on a white
      // card a transparent field is the card, and the only thing marking it
      // as somewhere to type was a hairline the same colour as the card's
      // own border. Focus draws the ring AND recolours the edge, so the
      // field in use is the one that stands out, not the one under it.
      "flex h-11 w-full rounded-lg border border-[var(--color-input)] bg-[var(--color-card)] px-3 py-1 text-base shadow-[var(--shadow-xs)] sm:h-9 sm:text-sm transition-[color,background-color,border-color,box-shadow] duration-150 placeholder:text-[var(--color-subtle-foreground)] hover:border-[var(--color-muted-foreground)] focus-visible:border-[var(--color-ring)] focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-[var(--color-ring)]/25 aria-invalid:border-[var(--color-destructive)] aria-invalid:ring-[var(--color-destructive)]/20 disabled:cursor-not-allowed disabled:opacity-60",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";
