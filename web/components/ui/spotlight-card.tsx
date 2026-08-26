"use client";

import * as React from "react";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * Card with a cursor-following radial-gradient highlight. Pure CSS variables
 * updated on mousemove — no dependencies. The tint uses the brand color at
 * low opacity so it stays subtle in dark mode.
 */
export function SpotlightCard({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  const ref = React.useRef<HTMLDivElement>(null);

  const onMouseMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    el.style.setProperty("--mx", `${e.clientX - rect.left}px`);
    el.style.setProperty("--my", `${e.clientY - rect.top}px`);
  };

  return (
    <Card
      ref={ref}
      onMouseMove={onMouseMove}
      className={cn("group relative overflow-hidden", className)}
      {...props}
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-300 group-hover:opacity-100"
        style={{
          background:
            "radial-gradient(200px circle at var(--mx, 50%) var(--my, 50%), color-mix(in srgb, var(--color-brand) 10%, transparent), transparent 70%)",
        }}
      />
      <div className="relative">{children}</div>
    </Card>
  );
}
