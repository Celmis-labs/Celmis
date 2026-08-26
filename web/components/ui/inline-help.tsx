"use client";

/**
 * A question answered where it is asked.
 *
 * Help on this app lived behind a "?" in the page header: one dialog holding
 * every explanation, opened by people who already knew there was something to
 * read. The questions themselves are local — "which branch does this audit?"
 * arrives at the scope card, "who writes the report?" at the engine picker —
 * and a header button is nowhere near either.
 *
 * So this is a plain <details>: one muted line that costs a row of vertical
 * space, opening in place to the same text the dialog holds. Native disclosure
 * rather than a popover, because it prints, it survives with JavaScript
 * disabled, Ctrl+F finds the closed text, and it needs no state.
 *
 * The header dialog stays — it is the walkthrough, read once. This is the
 * footnote, read at the moment of doubt.
 */

import { ChevronRightIcon, HelpCircleIcon } from "lucide-react";

import { cn } from "@/lib/utils";

export function InlineHelp({
  question,
  children,
  className,
}: {
  /** The question in the reader's words, not the feature's name. */
  question: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <details className={cn("group text-xs", className)}>
      <summary
        className={cn(
          "flex cursor-pointer list-none items-center gap-1.5 py-0.5",
          "text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]",
          // Safari draws its own triangle through ::-webkit-details-marker,
          // which list-none alone does not remove.
          "[&::-webkit-details-marker]:hidden",
        )}
      >
        <HelpCircleIcon className="h-3.5 w-3.5 shrink-0" />
        <span className="underline decoration-dotted underline-offset-2">{question}</span>
        <ChevronRightIcon className="h-3 w-3 shrink-0 transition-transform group-open:rotate-90" />
      </summary>
      {/* Indented to the text of the summary, so an open answer reads as
          belonging to its question rather than to the card. */}
      <div className="mt-1 max-w-3xl pl-5 leading-relaxed text-[var(--color-muted-foreground)]">
        {children}
      </div>
    </details>
  );
}
