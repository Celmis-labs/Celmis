/**
 * The four kinds of label a review screen shows, each with its own shape so
 * they can sit in one row without being mistaken for each other:
 *
 *   StatusPill     rounded-full, a leading dot   — where a run or stage IS
 *   SeverityBadge  square, an icon, upper-case   — how bad a finding is
 *   OriginTag      outline, an icon             — where a rule came from
 *   OverriddenPill outline, warm, a dot          — differs from the default
 *
 * Colour comes only from the status / severity scales in globals.css, and
 * every one carries a non-colour cue (dot, icon, words), so nothing is
 * colour-alone. Labels are passed in by the caller: the words are the
 * page's, translated where they are used.
 */

import {
  BookOpenIcon,
  BotIcon,
  CircleAlertIcon,
  FileDownIcon,
  InfoIcon,
  OctagonAlertIcon,
  SparklesIcon,
  TriangleAlertIcon,
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

// ─── lifecycle ──────────────────────────────────────────────────────────

export type RunStatus = "queued" | "running" | "success" | "partial" | "skipped" | "failed";

/** Every word the server uses for a run or stage, folded onto the scale.
 *  Unknown words are failures — the same rule the stage pills follow. */
export function toRunStatus(word: string | null | undefined): RunStatus {
  switch (word) {
    case "queued":
    case "pending":
      return "queued";
    case "running":
      return "running";
    case "complete":
    case "completed":
    case "success":
      return "success";
    case "partial":
      return "partial";
    case "skipped":
      return "skipped";
    default:
      return "failed";
  }
}

const STATUS_CLASS: Record<RunStatus, string> = {
  queued: "bg-[var(--color-status-queued-soft)] text-[var(--color-status-queued)]",
  running: "bg-[var(--color-status-running-soft)] text-[var(--color-status-running)]",
  success: "bg-[var(--color-status-success-soft)] text-[var(--color-status-success)]",
  partial: "bg-[var(--color-status-partial-soft)] text-[var(--color-status-partial)]",
  skipped: "bg-[var(--color-status-skipped-soft)] text-[var(--color-status-skipped)]",
  failed: "bg-[var(--color-status-failed-soft)] text-[var(--color-status-failed)]",
};

export function StatusDot({ status, className }: { status: RunStatus; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block size-1.5 shrink-0 rounded-full bg-current",
        // Queued is a hollow ring: it has not started, so it is not "filled".
        status === "queued" && "bg-transparent ring-1 ring-current",
        status === "running" && "animate-pulse-dot",
        className,
      )}
    />
  );
}

export function StatusPill({
  status,
  label,
  className,
  title,
}: {
  status: RunStatus;
  label: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium leading-4",
        STATUS_CLASS[status],
        className,
      )}
    >
      <StatusDot status={status} />
      {label}
    </span>
  );
}

// ─── severity ───────────────────────────────────────────────────────────

export type Severity = "critical" | "error" | "warning" | "info";
export const SEVERITIES: Severity[] = ["critical", "error", "warning", "info"];

export function toSeverity(word: string | null | undefined): Severity {
  return word === "critical" || word === "error" || word === "warning" ? word : "info";
}

const SEVERITY_ICON: Record<Severity, LucideIcon> = {
  critical: OctagonAlertIcon,
  error: TriangleAlertIcon,
  warning: CircleAlertIcon,
  info: InfoIcon,
};

/** Text colour for a severity, for a count or a word outside a badge. */
export const SEVERITY_TEXT: Record<Severity, string> = {
  critical: "text-[var(--color-sev-critical)]",
  error: "text-[var(--color-sev-error)]",
  warning: "text-[var(--color-sev-warning)]",
  info: "text-[var(--color-sev-info)]",
};

const SEVERITY_CLASS: Record<Severity, string> = {
  critical: "bg-[var(--color-sev-critical-soft)] text-[var(--color-sev-critical)]",
  error: "bg-[var(--color-sev-error-soft)] text-[var(--color-sev-error)]",
  warning: "bg-[var(--color-sev-warning-soft)] text-[var(--color-sev-warning)]",
  info: "bg-[var(--color-sev-info-soft)] text-[var(--color-sev-info)]",
};

/** CSS colour of a severity, for chart marks. */
export const SEVERITY_COLOR: Record<Severity, string> = {
  critical: "var(--color-sev-critical)",
  error: "var(--color-sev-error)",
  warning: "var(--color-sev-warning)",
  info: "var(--color-sev-info)",
};

export function SeverityIcon({ severity, className }: { severity: Severity; className?: string }) {
  const Icon = SEVERITY_ICON[severity];
  return <Icon aria-hidden className={cn("size-3 shrink-0", className)} />;
}

export function SeverityBadge({
  severity,
  label,
  className,
}: {
  severity: Severity;
  label: React.ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[11px] font-semibold uppercase leading-4 tracking-wide",
        SEVERITY_CLASS[severity],
        className,
      )}
    >
      <SeverityIcon severity={severity} />
      {label}
    </span>
  );
}

/** "2 ⬣ 1 ▲ 4 ●" — a finding count per severity, zeros left out. Each count
 *  carries its icon and a screen-reader word, so the row is scannable and
 *  still says what it counts. */
export function SeverityCounts({
  counts,
  labels,
  className,
}: {
  counts: Partial<Record<Severity, number>>;
  labels: Record<Severity, string>;
  className?: string;
}) {
  const shown = SEVERITIES.filter((s) => (counts[s] ?? 0) > 0);
  if (shown.length === 0) return null;
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-1", className)}>
      {shown.map((s) => (
        <span
          key={s}
          title={`${labels[s]}: ${counts[s]}`}
          className={cn(
            "inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-semibold tabular-nums leading-4",
            SEVERITY_CLASS[s],
          )}
        >
          <SeverityIcon severity={s} />
          {counts[s]}
          <span className="sr-only">{labels[s]}</span>
        </span>
      ))}
    </span>
  );
}

// ─── origin ─────────────────────────────────────────────────────────────

const ORIGIN_ICON: Record<string, LucideIcon> = {
  library: BookOpenIcon,
  generated: SparklesIcon,
  imported: FileDownIcon,
  agent: BotIcon,
};

export function OriginTag({
  origin,
  label,
  className,
}: {
  origin: string;
  label: React.ReactNode;
  className?: string;
}) {
  const Icon = ORIGIN_ICON[origin];
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[11px] font-medium leading-4",
        // The library is the one curated source, so it alone gets a hue.
        origin === "library"
          ? "border-[var(--color-info)]/35 text-[var(--color-info)]"
          : "border-[var(--color-border-strong)] text-[var(--color-muted-foreground)]",
        className,
      )}
    >
      {Icon && <Icon aria-hidden className="size-3" />}
      {label}
    </span>
  );
}

// ─── changed from the default ───────────────────────────────────────────

export function OverriddenPill({
  label,
  className,
  title,
}: {
  label: React.ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border border-[var(--color-attention)]/40 bg-[var(--color-attention-soft)] px-2 py-0.5 text-[11px] font-medium leading-4 text-[var(--color-attention)]",
        className,
      )}
    >
      <span aria-hidden className="size-1.5 rounded-full bg-current" />
      {label}
    </span>
  );
}
