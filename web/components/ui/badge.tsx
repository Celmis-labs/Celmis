import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * A short label on a tint. Every tone is text-on-its-own-tint at ≥4.5:1 in
 * both themes (see globals.css), so a 12px word stays readable.
 *
 * A <span>, not a <div>: badges sit inside buttons and sentences, where a
 * block element is invalid markup.
 *
 * For lifecycle, severity, origin and "changed from default" use the
 * purpose-built pills in ./status — they carry an icon or a dot as well as a
 * colour, so the meaning survives without colour vision.
 */
const badgeVariants = cva(
  "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md border px-2 py-0.5 text-xs font-medium leading-4 transition-colors [&_svg]:size-3 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-[var(--color-neutral-soft)] text-[var(--color-muted-foreground)]",
        success:
          "border-transparent bg-[var(--color-success-soft)] text-[var(--color-success)]",
        warning:
          "border-transparent bg-[var(--color-warning-soft)] text-[var(--color-warning)]",
        destructive:
          "border-transparent bg-[var(--color-destructive-soft)] text-[var(--color-destructive)]",
        info:
          "border-transparent bg-[var(--color-info-soft)] text-[var(--color-info)]",
        attention:
          "border-transparent bg-[var(--color-attention-soft)] text-[var(--color-attention)]",
        outline:
          "border-[var(--color-border-strong)] text-[var(--color-muted-foreground)]",
        brand:
          "border-transparent bg-[var(--color-primary-soft)] text-[var(--color-primary-soft-foreground)]",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>["variant"]>;

export function Badge({
  className,
  variant,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { badgeVariants };
