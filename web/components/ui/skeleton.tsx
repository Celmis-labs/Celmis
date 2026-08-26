import { cn } from "@/lib/utils";

/** Loading placeholder block — size it via className, e.g. `h-4 w-32`. */
export function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse rounded bg-[var(--color-muted)]/40", className)}
      {...props}
    />
  );
}
