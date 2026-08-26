import { cn } from "@/lib/utils";

export function BrandMark({
  className,
  size = "md",
}: {
  className?: string;
  size?: "sm" | "md" | "lg";
}) {
  const dim =
    size === "sm" ? "h-7 w-7 text-sm" : size === "lg" ? "h-12 w-12 text-xl" : "h-9 w-9 text-base";
  return (
    <div
      className={cn(
        "flex items-center justify-center rounded-lg bg-[var(--color-brand)] font-bold text-[var(--color-brand-foreground)] shadow-sm",
        dim,
        className,
      )}
      aria-label="Celmis"
    >
      Cl
    </div>
  );
}

export function BrandWord({ className }: { className?: string }) {
  return (
    <span
      className={cn("font-semibold tracking-tight", className)}
      style={{ fontFeatureSettings: '"ss01"' }}
    >
      Celmis
    </span>
  );
}
