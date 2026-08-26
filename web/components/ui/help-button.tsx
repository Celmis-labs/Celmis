"use client";
import { HelpCircleIcon } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * The little "?" next to a section title.
 *
 * It was a bare <button> around a 16px icon, and Tailwind's preflight zeroes
 * button padding — so the tap target was 16px, roughly 4mm against a 9mm
 * fingertip, and on the Claude page it gated the whole connect flow. The
 * negative margins buy a 44px box without moving the icon a pixel.
 */
export const HelpButton = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement>
>(({ className, ...props }, ref) => (
  <button
    ref={ref}
    type="button"
    className={cn(
      "-my-3 -mx-3 grid size-11 shrink-0 place-items-center rounded-md",
      "text-[var(--color-muted-foreground)] transition-colors",
      "hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]",
      className,
    )}
    {...props}
  >
    <HelpCircleIcon className="h-4 w-4" />
  </button>
));
HelpButton.displayName = "HelpButton";
