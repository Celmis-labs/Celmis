"use client";

/**
 * Page layout primitives.
 *
 * Pages had drifted into six different container widths (3xl–6xl) and three
 * spacing rhythms, so moving between them felt like moving between apps. There
 * are really only two kinds of page here, so there are two widths:
 *
 *   "standard" — forms, settings, admin panels. Comfortable reading measure.
 *   "wide"     — lists, tables, diffs, and every page with SectionTabs.
 *
 * Everything else (padding, vertical rhythm) is fixed on purpose.
 */

import { cn } from "@/lib/utils";

const WIDTHS = {
  // A reading measure. Prose and forms get worse past roughly 90 characters
  // a line, so this one does NOT grow with the monitor.
  standard: "max-w-5xl",
  // Lists, tables, diffs and every page with SectionTabs. This was max-w-6xl
  // (1152px), which on a 2200px screen left the content using under half the
  // width with the page title floating in the middle of a 400px gutter.
  //
  // Raising it is not only about filling space: the header rows here are
  // responsive, so with the extra width the page controls move up beside the
  // title instead of taking their own row, help text stops wrapping to two
  // lines, and a screenful shows the content instead of the chrome.
  //
  // Below 1152px nothing changes — this only raises a ceiling.
  wide: "max-w-[1680px]",
} as const;

export function PageShell({
  children,
  width = "standard",
  className,
}: {
  children: React.ReactNode;
  width?: keyof typeof WIDTHS;
  className?: string;
}) {
  return (
    <div className={cn("mx-auto w-full p-4 sm:p-8 space-y-6", WIDTHS[width], className)}>
      {children}
    </div>
  );
}

/**
 * Title + description + optional right-aligned actions + optional tab row.
 *
 * The breadcrumb bar already says *where* you are, so this says *what this
 * page does* — one sentence, present tense, no marketing. `tabs` takes a
 * <SectionTabs> row that renders full-width under the title block.
 */
export function PageHeader({
  title,
  description,
  icon,
  badge,
  actions,
  tabs,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  icon?: React.ReactNode;
  /** Small inline marker next to the title, e.g. <WorkspaceBadge/>. */
  badge?: React.ReactNode;
  actions?: React.ReactNode;
  /** A <SectionTabs> row rendered under the title block. */
  tabs?: React.ReactNode;
}) {
  return (
    <div>
      {/* One column on a phone. 390px cannot hold a title that already needs
          three lines, a workspace name and a "New …" button on one row: the
          title floors at its longest word and the other two overflow the
          column and land on top of it. The desktop single row returns at sm. */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
        <div className="min-w-0">
          {/* Wrapping is what keeps the badge off the title: flex breaks lines
              on each item's *unshrunk* width, so a title too long for one line
              carries the badge down with it, while a short title keeps the
              badge inline the way it has always been. */}
          <h1 className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-2xl font-semibold tracking-tight">
            {/* Icon and title are one unit, or wrapping strands the icon alone
                on the line above the title it belongs to. min-w-0 twice
                because a flex item refuses to shrink past its longest word by
                default — the title has to be allowed to wrap, or it pushes
                everything after it out of the page. */}
            <span className="flex min-w-0 items-center gap-2">
              {icon}
              <span className="min-w-0 break-words">{title}</span>
            </span>
            {/* A workspace name is user data of arbitrary length, so it is
                clamped to the line and cut off. Anything else lets one long
                name decide the width of every header in the app. */}
            {badge && (
              <span className="flex min-w-0 max-w-full items-center [&>*]:min-w-0 [&>*]:max-w-full [&>*]:truncate">
                {badge}
              </span>
            )}
          </h1>
          {description && (
            <p className="mt-1 text-sm text-[var(--color-muted-foreground)]">
              {description}
            </p>
          )}
        </div>
        {/* Right-aligned on its own row below the title on phones, where the
            row is full width; at sm the box is content-sized again, so
            justify-end and the wrap are inert and the desktop row is intact.
            Sizing is left to the callers — some pass an icon-only button that
            would look absurd stretched across the screen. */}
        {actions && (
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">{actions}</div>
        )}
      </div>
      {tabs && <div className="mt-4">{tabs}</div>}
    </div>
  );
}
