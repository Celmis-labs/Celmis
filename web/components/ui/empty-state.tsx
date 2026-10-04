import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * "Nothing here yet", said usefully: what this place will hold, and the one
 * thing to do about it. The icon sits on a soft tile so the block reads as
 * a deliberate state, not as a gap where content failed to load.
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  children,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
  /** Extra guidance under the description (a setup hint, a link). */
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 px-4 py-12 text-center", className)}>
      {Icon && (
        <span className="grid size-11 place-items-center rounded-xl border border-[var(--color-border)] bg-[var(--color-muted)] text-[var(--color-muted-foreground)] shadow-[var(--shadow-xs)]">
          <Icon aria-hidden className="size-5" />
        </span>
      )}
      <div className="space-y-1">
        <div className="text-sm font-semibold text-[var(--color-foreground)]">{title}</div>
        {description && (
          <p className="mx-auto max-w-md text-sm leading-relaxed text-[var(--color-muted-foreground)]">
            {description}
          </p>
        )}
      </div>
      {children}
      {action && <div className="mt-1 flex flex-wrap justify-center gap-2">{action}</div>}
    </div>
  );
}
