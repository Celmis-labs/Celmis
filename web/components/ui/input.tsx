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
      "flex h-11 w-full rounded-md border border-[var(--color-input)] bg-[var(--color-background)] px-3 py-1 text-base shadow-[var(--shadow-xs)] sm:h-9 sm:text-sm transition-[color,background-color,border-color,box-shadow] placeholder:text-[var(--color-muted-foreground)]/80 hover:border-[var(--color-muted-foreground)]/40 focus-visible:border-[var(--color-ring)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]/30 disabled:cursor-not-allowed disabled:opacity-60",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";
