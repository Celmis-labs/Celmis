import { cn } from "@/lib/utils";

/**
 * Loading placeholder block — size it via className, e.g. `h-4 w-32`.
 *
 * A tint that is visible against the card (the old 40% muted was 1.03:1, so
 * a loading list looked like an empty one) with a slow light sweep across
 * it. Under reduced motion the sweep stops and the tint stays.
 */
export function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden
      className={cn(
        "animate-shimmer rounded-md bg-[var(--color-muted)] bg-[length:200%_100%]",
        "bg-[linear-gradient(90deg,transparent_0%,color-mix(in_oklab,var(--color-foreground)_6%,transparent)_50%,transparent_100%)]",
        "ring-1 ring-inset ring-[var(--color-border)]/60",
        className,
      )}
      {...props}
    />
  );
}

/** Skeleton rows shaped like a list: a short lead cell, a long title and a
 *  pill at the end, so the page does not jump when the data lands. */
export function SkeletonRows({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div role="status" aria-busy="true" className={cn("divide-y divide-[var(--color-border)]", className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 py-3">
          <Skeleton className="h-4 w-10 shrink-0" />
          <div className="min-w-0 flex-1 space-y-1.5">
            <Skeleton className="h-3.5" style={{ width: `${70 - ((i * 17) % 35)}%` }} />
            <Skeleton className="h-3 w-1/4" />
          </div>
          <Skeleton className="h-5 w-16 shrink-0 rounded-full" />
        </div>
      ))}
    </div>
  );
}
