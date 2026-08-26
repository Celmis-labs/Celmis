import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-semibold transition-colors",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-[var(--color-secondary)] text-[var(--color-secondary-foreground)]",
        success:
          "border-transparent bg-[var(--color-success)]/15 text-[var(--color-success)]",
        warning:
          "border-transparent bg-[var(--color-warning)]/20 text-[var(--color-warning)]",
        destructive:
          "border-transparent bg-[var(--color-destructive)]/15 text-[var(--color-destructive)]",
        outline:
          "border-[var(--color-border)] text-[var(--color-foreground)]",
        brand:
          "border-transparent bg-[var(--color-brand-muted)] text-[var(--color-brand)]",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export function Badge({
  className,
  variant,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof badgeVariants>) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}
