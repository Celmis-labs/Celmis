"use client";

import { useEffect, useState } from "react";

/**
 * Animates a number from 0 to `value` over ~600ms with an ease-out curve.
 * Respects prefers-reduced-motion (renders the final value immediately).
 */
export function CountUp({
  value,
  duration = 600,
  className,
}: {
  value: number;
  duration?: number;
  className?: string;
}) {
  const [display, setDisplay] = useState(0);

  useEffect(() => {
    const target = Number.isFinite(value) ? value : 0;
    let reduced = false;
    try {
      reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch {
      /* no matchMedia — animate anyway */
    }
    // Reduced motion → a single frame that jumps straight to the target.
    const dur = reduced ? 0 : duration;
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const p = dur <= 0 ? 1 : Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - p, 3); // easeOutCubic
      setDisplay(Math.round(target * eased));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);

  return <span className={className}>{display}</span>;
}
