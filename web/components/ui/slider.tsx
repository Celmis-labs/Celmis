"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * A single-value slider. A native <input type="range"> (keyboard, touch and
 * screen readers come with it) drawn with the app's track, fill and thumb —
 * see `.celmis-range` in globals.css. The filled part is a CSS variable, so
 * dragging re-renders nothing but this input.
 */
export const Slider = React.forwardRef<
  HTMLInputElement,
  Omit<React.InputHTMLAttributes<HTMLInputElement>, "type" | "value" | "onChange"> & {
    value: number;
    min?: number;
    max?: number;
    step?: number;
    onValueChange: (v: number) => void;
  }
>(({ value, min = 0, max = 100, step = 1, onValueChange, className, style, ...props }, ref) => {
  const pct = max > min ? ((value - min) / (max - min)) * 100 : 0;
  return (
    <input
      ref={ref}
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      onChange={(e) => onValueChange(Number(e.target.value))}
      className={cn("celmis-range", className)}
      style={{ ...style, "--range-pct": `${Math.min(100, Math.max(0, pct))}%` } as React.CSSProperties}
      {...props}
    />
  );
});
Slider.displayName = "Slider";
