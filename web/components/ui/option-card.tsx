"use client";
import * as React from "react";
import { CheckIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * A selectable card: a choice that needs a sentence of explanation, not just
 * a word (a review mode, a library rule, "add as active / pending").
 *
 * It is a native radio or checkbox inside a <label>, visually hidden, so the
 * keyboard model is the browser's: Tab into a radio group, arrows move the
 * choice, Space toggles a checkbox. The card shows the state three ways —
 * border, tint and a check mark in the corner — so it never depends on
 * colour alone.
 *
 *   <OptionCardGroup label="Review mode">
 *     <OptionCard type="radio" name="mode" value="light" checked={…}
 *       onCheckedChange={() => set("light")} title="Light" description="…" />
 *   </OptionCardGroup>
 */
export function OptionCard({
  type = "radio",
  name,
  value,
  checked,
  onCheckedChange,
  disabled,
  title,
  description,
  icon,
  meta,
  children,
  className,
}: {
  type?: "radio" | "checkbox";
  name?: string;
  value?: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Leading icon, drawn in a tinted tile. */
  icon?: React.ReactNode;
  /** Badges after the title (severity, "already added", …). */
  meta?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <label
      className={cn(
        "group relative flex cursor-pointer items-start gap-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-3 pr-10 text-sm",
        "transition-[border-color,background-color,box-shadow] duration-150 ease-out-quint",
        "hover:border-[var(--color-border-strong)] hover:bg-[var(--color-accent)]/50",
        "has-[input:checked]:border-[var(--color-primary)] has-[input:checked]:bg-[var(--color-primary-soft)]/45 has-[input:checked]:shadow-[0_0_0_1px_var(--color-primary)]",
        "has-[input:focus-visible]:ring-2 has-[input:focus-visible]:ring-[var(--color-ring)] has-[input:focus-visible]:ring-offset-2 has-[input:focus-visible]:ring-offset-[var(--color-background)]",
        "has-[input:disabled]:cursor-not-allowed has-[input:disabled]:opacity-60",
        className,
      )}
    >
      <input
        type={type}
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={(e) => onCheckedChange(e.target.checked)}
        className="peer sr-only"
      />
      {icon && (
        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-[var(--color-muted)] text-[var(--color-muted-foreground)] transition-colors group-has-[input:checked]:bg-[var(--color-primary)] group-has-[input:checked]:text-[var(--color-primary-foreground)] [&_svg]:size-4">
          {icon}
        </span>
      )}
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-medium text-[var(--color-foreground)]">{title}</span>
          {meta}
        </span>
        {description && (
          <span className="mt-0.5 block text-xs leading-relaxed text-[var(--color-muted-foreground)]">
            {description}
          </span>
        )}
        {children}
      </span>
      {/* The corner mark: a ring when free, a filled check when chosen. */}
      <span
        aria-hidden
        className={cn(
          "absolute right-3 top-3 grid size-5 place-items-center border border-[var(--color-input)] bg-[var(--color-card)] text-[var(--color-primary-foreground)] transition-[background-color,border-color] duration-150",
          type === "radio" ? "rounded-full" : "rounded-md",
          "group-has-[input:checked]:border-[var(--color-primary)] group-has-[input:checked]:bg-[var(--color-primary)]",
        )}
      >
        <CheckIcon
          strokeWidth={3}
          className="size-3 scale-50 opacity-0 transition-[opacity,transform] duration-200 ease-out-quint group-has-[input:checked]:scale-100 group-has-[input:checked]:opacity-100"
        />
      </span>
    </label>
  );
}

/** A labelled group of OptionCards, laid out in a responsive grid. */
export function OptionCardGroup({
  label,
  type = "radio",
  columns = 1,
  className,
  children,
}: {
  label: string;
  type?: "radio" | "checkbox";
  columns?: 1 | 2 | 3;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role={type === "radio" ? "radiogroup" : "group"}
      aria-label={label}
      className={cn(
        "grid gap-2",
        columns === 2 && "sm:grid-cols-2",
        columns === 3 && "sm:grid-cols-2 lg:grid-cols-3",
        className,
      )}
    >
      {children}
    </div>
  );
}
