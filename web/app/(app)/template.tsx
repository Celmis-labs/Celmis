"use client";

import * as m from "motion/react-m";

/**
 * A short fade as each page arrives.
 *
 * A template rather than the layout: Next mounts a template afresh on every
 * navigation, which is exactly the moment to animate, while the layout — and
 * the sidebar, the top bar and the agent panel in it — stays put.
 *
 * Opacity only. A transform here would make this box the containing block
 * for every `position: fixed` element a page renders (drawers, sheets,
 * click-away layers), which is the bug the top bar's backdrop-filter has
 * already caused twice. The flex column is what <main> gave its children
 * before this wrapper existed, so pages that fill the height still do.
 */
export default function AppTemplate({ children }: { children: React.ReactNode }) {
  return (
    <m.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
      className="flex min-h-0 flex-1 flex-col"
    >
      {children}
    </m.div>
  );
}
