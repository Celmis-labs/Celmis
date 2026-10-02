"use client";
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  // A press is felt as well as seen: 2% smaller while held. Transform only,
  // so nothing around the button reflows, and `motion-reduce` keeps it still.
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-[color,background-color,border-color,box-shadow,opacity,transform] duration-150 active:scale-[0.98] motion-reduce:active:scale-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--color-background)] disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default:
          "bg-[var(--color-brand)] text-[var(--color-brand-foreground)] shadow-[var(--shadow-xs)] hover:opacity-90",
        secondary:
          "bg-[var(--color-secondary)] text-[var(--color-secondary-foreground)] hover:bg-[var(--color-accent)]",
        // The field's edge colour, not the card's: an outline button beside an
        // input is read as the same family of control, and with the lighter
        // border it was the faintest thing in the row (LLM settings, Save).
        outline:
          "border border-[var(--color-input)] bg-[var(--color-background)] text-[var(--color-foreground)] shadow-[var(--shadow-xs)] hover:border-[var(--color-muted-foreground)]/40 hover:bg-[var(--color-accent)]",
        // Muted until hovered, so a row of actions has one loud button and a
        // quiet one — but the quiet one is still legibly a button.
        ghost:
          "text-[var(--color-foreground)]/80 hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]",
        destructive:
          "bg-[var(--color-destructive)] text-[var(--color-destructive-foreground)] hover:opacity-90",
        link: "text-[var(--color-brand)] underline-offset-4 hover:underline",
      },
      size: {
        sm: "h-11 px-3 sm:h-8",
        default: "h-11 px-4 sm:h-9",
        lg: "h-10 px-6 text-base",
        icon: "h-11 w-11 sm:h-9 sm:w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  ),
);
Button.displayName = "Button";

export { buttonVariants };
