"use client";

/**
 * A small styled single-select built on the Radix dropdown menu, so it matches
 * the app's theme instead of falling back to the native OS <select> chrome.
 */

import { CheckIcon, ChevronDownIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./dropdown-menu";

export type SelectOption = {
  value: string;
  label: string;
  /** Heading this option sits under. Options carrying the same group are
   *  listed together, in first-seen order; a list where nothing sets it
   *  renders exactly as before. */
  group?: string;
  /** Muted text after the label — a count, a state, anything secondary. */
  hint?: string;
  /** Listed but not choosable, struck through. For a value that EXISTS and
   *  must stay visible — a reasoning level the provider refused — rather than
   *  one that should not be offered at all. Silently omitting it is how an
   *  option vanished from a dropdown between two page loads with no reason,
   *  no date and no remedy; a struck row with a hint says what happened. It
   *  can still be the current value: the trigger shows whatever is selected,
   *  and a saved value is not unsaved by becoming unselectable. */
  disabled?: boolean;
};

/** Options in first-seen group order. Ungrouped options keep a null heading,
 *  which is how a plain list stays a plain list. */
function groupOptions(options: SelectOption[]): Array<[string | null, SelectOption[]]> {
  const order: Array<string | null> = [];
  const byGroup = new Map<string | null, SelectOption[]>();
  for (const o of options) {
    const g = o.group ?? null;
    if (!byGroup.has(g)) {
      byGroup.set(g, []);
      order.push(g);
    }
    byGroup.get(g)!.push(o);
  }
  return order.map((g) => [g, byGroup.get(g)!]);
}

export function Select({
  value,
  onChange,
  options,
  className,
  placeholder = "Select…",
  disabled = false,
  id,
}: {
  value: string;
  onChange: (v: string) => void;
  options: SelectOption[];
  className?: string;
  placeholder?: string;
  disabled?: boolean;
  /** Lands on the trigger button so a <Label htmlFor> actually binds to it. */
  id?: string;
}) {
  const current = options.find((o) => o.value === value);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <button
          type="button"
          id={id}
          disabled={disabled}
          className={cn(
            "flex h-11 items-center justify-between gap-2 rounded-md border border-[var(--color-border)] bg-transparent px-3 text-base sm:h-9 sm:text-sm transition-colors hover:bg-[var(--color-accent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand)] disabled:cursor-not-allowed disabled:opacity-50",
            className,
          )}
        >
          <span className={cn(
            "truncate",
            !current && "text-[var(--color-muted-foreground)]",
            // The current value is one the list no longer lets you choose —
            // shown struck on the trigger too, so the conflict is visible
            // with the menu closed.
            current?.disabled && "line-through",
          )}>
            {current?.label ?? placeholder}
          </span>
          <ChevronDownIcon className="h-3.5 w-3.5 shrink-0 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        className="max-h-[60vh] min-w-[var(--radix-dropdown-menu-trigger-width)] overflow-y-auto"
      >
        {groupOptions(options).map(([group, items], gi) => (
          // aria-label rather than a label/aria-labelledby pair: the heading is
          // decorative markup inside the menu, and naming the group directly is
          // what a screen reader announces on entering it.
          <DropdownMenuGroup key={group ?? `__${gi}`} aria-label={group ?? undefined}>
            {group && (
              <>
                {gi > 0 && <DropdownMenuSeparator />}
                <DropdownMenuLabel className="text-xs font-normal text-[var(--color-muted-foreground)]">
                  {group}
                </DropdownMenuLabel>
              </>
            )}
            {items.map((o) => (
              <DropdownMenuItem
                key={o.value}
                disabled={o.disabled}
                onSelect={() => onChange(o.value)}
                className="flex items-center justify-between gap-6"
              >
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className={cn("truncate", o.disabled && "line-through")}>{o.label}</span>
                  {o.hint && (
                    <span className="shrink-0 text-xs text-[var(--color-muted-foreground)]">
                      {o.hint}
                    </span>
                  )}
                </span>
                {o.value === value && (
                  <CheckIcon className="h-3.5 w-3.5 shrink-0 text-[var(--color-brand)]" />
                )}
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
