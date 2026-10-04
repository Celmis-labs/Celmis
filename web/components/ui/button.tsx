"use client";
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2Icon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * One button, five weights. Pick by consequence, not by looks:
 *
 *   default      the ONE action this view exists for (Save, Run review, Add).
 *                At most one per view; a second filled button is a second
 *                answer to "what do I do here".
 *   secondary    a solid neutral: the bulk-bar actions, a second "do"
 *                that is not the point of the page.
 *   outline      the workhorse: everyday commands next to content (Retry,
 *                Previous / Next, Re-run, Approve on a row).
 *   ghost        quiet: Cancel, toggles, icon buttons in a toolbar.
 *   destructive  removes something for good. Paired with a confirm.
 *   link         inline navigation that happens to be a button.
 *
 * `loading` keeps the width, disables the button and swaps the leading icon
 * for a spinner, so a pending save cannot be pressed twice and the label
 * does not jump. Icons are sized by the button, so call sites pass a bare
 * <Icon /> and never a margin.
 */
const buttonVariants = cva(
  // A press is felt as well as seen: 2% smaller while held. Transform only,
  // so nothing around the button reflows, and `motion-reduce` keeps it still.
  [
    "relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium",
    "transition-[color,background-color,border-color,box-shadow,transform] duration-150 ease-out-quint",
    "active:scale-[0.98] motion-reduce:active:scale-100",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-background)]",
    "disabled:pointer-events-none disabled:opacity-50 aria-busy:cursor-progress",
    "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-']):not([class*='_h-'])]:size-4",
  ].join(" "),
  {
    variants: {
      variant: {
        default:
          "bg-[var(--color-primary)] text-[var(--color-primary-foreground)] shadow-[var(--shadow-xs),inset_0_1px_0_0_rgb(255_255_255/0.12)] hover:bg-[var(--color-primary-hover)]",
        secondary:
          "bg-[var(--color-secondary)] text-[var(--color-secondary-foreground)] shadow-[var(--shadow-xs)] hover:bg-[var(--color-selected)]",
        // The field's edge colour, not the card's: an outline button beside an
        // input is read as the same family of control, and with the lighter
        // border it was the faintest thing in the row (LLM settings, Save).
        outline:
          "border border-[var(--color-border-strong)] bg-[var(--color-card)] text-[var(--color-foreground)] shadow-[var(--shadow-xs)] hover:border-[var(--color-input)] hover:bg-[var(--color-accent)]",
        // Muted until hovered, so a row of actions has one loud button and a
        // quiet one — but the quiet one is still legibly a button.
        ghost:
          "text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]",
        destructive:
          "bg-[var(--color-danger-solid)] text-white shadow-[var(--shadow-xs)] hover:bg-[var(--color-danger-solid-hover)] focus-visible:ring-[var(--color-destructive)]",
        link: "h-auto px-0 text-[var(--color-primary)] underline-offset-4 hover:underline",
      },
      size: {
        xs: "h-9 gap-1.5 px-2.5 text-xs sm:h-7",
        sm: "h-11 gap-1.5 px-3 sm:h-8",
        default: "h-11 px-4 sm:h-9",
        lg: "h-11 px-6 text-base sm:h-10",
        icon: "h-11 w-11 sm:h-9 sm:w-9",
        "icon-sm": "h-11 w-11 sm:h-8 sm:w-8",
      },
    },
    compoundVariants: [
      { variant: "link", size: ["sm", "default", "lg", "xs"], class: "h-auto px-0 sm:h-auto" },
    ],
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** In flight: disabled, `aria-busy`, and a spinner in place of the first
   *  icon (or before the label when there is none). */
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, loading = false, disabled, children, ...props }, ref) => {
    let content = children;
    if (loading) {
      const parts = React.Children.toArray(children);
      const first = parts[0];
      const leadingIcon = React.isValidElement(first) && typeof first.type !== "string";
      const spinner = <Loader2Icon key="__spinner" aria-hidden className="animate-spin" />;
      content = leadingIcon ? [spinner, ...parts.slice(1)] : [spinner, ...parts];
    }
    return (
      <button
        ref={ref}
        className={cn(buttonVariants({ variant, size }), className)}
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        {...props}
      >
        {content}
      </button>
    );
  },
);
Button.displayName = "Button";

export { buttonVariants };
