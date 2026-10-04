import * as React from "react";

import { cn } from "@/lib/utils";

export interface TextareaProps
  extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {}

const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        "flex min-h-[80px] w-full rounded-lg border border-[var(--color-input)] bg-[var(--color-card)] px-3 py-2 text-base shadow-[var(--shadow-xs)] sm:text-sm",
        "transition-[border-color,box-shadow] duration-150 placeholder:text-[var(--color-subtle-foreground)] hover:border-[var(--color-muted-foreground)]",
        "focus-visible:border-[var(--color-ring)] focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-[var(--color-ring)]/25",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "resize-none",
        className,
      )}
      {...props}
    />
  ),
);
Textarea.displayName = "Textarea";

export { Textarea };
