import type { LucideIcon } from "lucide-react";

/** Centered "nothing here yet" block with optional description and CTA. */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
}: {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      {Icon && <Icon className="h-8 w-8 text-[var(--color-muted-foreground)]" />}
      <div className="text-sm font-medium">{title}</div>
      {description && (
        <p className="max-w-sm text-xs text-[var(--color-muted-foreground)]">{description}</p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
