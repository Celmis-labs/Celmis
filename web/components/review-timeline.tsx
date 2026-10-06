"use client";

/**
 * A pull request's reviews, each expandable into its stages — the Kodus-style
 * timeline: "Review 2 · Skipped · Duration 3m 18s · 3 days ago", and under it
 * every stage with a status pill, its duration, when it started and why.
 *
 * Motion carries state, nothing else: a review opens by growing to its
 * height, and its stages arrive top to bottom in a short cascade (capped, so
 * a 20-stage run does not make anyone wait), which is the order they ran
 * in. Under reduced motion both are a plain fade.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import {
  CheckIcon, ChevronRightIcon, LoaderIcon, MinusIcon, XIcon,
} from "lucide-react";

import { pullRequestsApi, type PullRequestCommand, type ReviewRunOut } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import {
  formatDuration, metaEntries, relativeTime, runDurationMs, stageLabel,
  stageStatusWord, type ReviewStage, type StageStatus,
} from "@/lib/review-stages";
import { useToken } from "@/lib/use-token";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { QueryState } from "@/components/ui/query-state";
import { StatusPill, toRunStatus, type RunStatus } from "@/components/ui/status";

/** A run's lifecycle word as a badge variant — for call sites still on
 *  <Badge>. New code uses <StatusPill status={toRunStatus(word)} />. */
export const RUN_VARIANT: Record<string, "success" | "warning" | "default" | "destructive" | "info"> = {
  complete: "success",
  partial: "warning",
  skipped: "default",
  failed: "destructive",
  queued: "default",
  running: "info",
};

const ACTIVE = new Set(["queued", "running"]);
const EASE = [0.23, 1, 0.32, 1] as const;

export function PullRequestReviews({ prId }: { prId: string }) {
  const token = useToken();
  const { t } = useI18n();
  const runs = useQuery({
    queryKey: ["pr-runs", prId],
    queryFn: () => pullRequestsApi.runs(token!, prId),
    enabled: !!token,
    // A review still in flight fills its stages in as it goes.
    refetchInterval: (q) =>
      (q.state.data?.items ?? []).some((r) => ACTIVE.has(r.status ?? "")) ? 3000 : false,
  });
  return (
    <QueryState query={runs} skeleton={2}>
      {(data: { items: ReviewRunOut[] }) =>
        data.items.length === 0 ? (
          <p className="py-2 text-sm text-[var(--color-muted-foreground)]">{t("prs.noRuns")}</p>
        ) : (
          <ol className="flex flex-col gap-2">
            {data.items.map((run, idx) => (
              <ReviewRunItem key={run.id} run={run} ordinal={data.items.length - idx}
                defaultOpen={idx === 0} />
            ))}
          </ol>
        )
      }
    </QueryState>
  );
}

/** What became of a comment command, as a badge variant. */
const COMMAND_VARIANT: Record<string, "success" | "warning" | "default" | "destructive" | "info"> = {
  done: "success",
  claimed: "info",
  failed: "destructive",
  denied: "warning",
  rate_limited: "warning",
  ignored: "default",
};

/** The `@celmis ...` comments given on this pull request, newest first —
 *  rendered under its reviews. Renders nothing while there are none, so a PR
 *  nobody commanded looks as it always did. */
export function PullRequestCommands({ prId }: { prId: string }) {
  const token = useToken();
  const { t, locale } = useI18n();
  const commands = useQuery({
    queryKey: ["pr-commands", prId],
    queryFn: () => pullRequestsApi.commands(token!, prId),
    enabled: !!token,
  });
  const items: PullRequestCommand[] = commands.data?.items ?? [];
  if (items.length === 0) return null;
  return (
    <section className="mt-3" aria-label={t("prs.commands.title")}>
      <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)]">
        {t("prs.commands.title")}
      </h4>
      <ul className="flex flex-col gap-1.5">
        {items.map((c) => (
          <li key={c.id} className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-sm">
            <span className="font-medium">
              {t(`prs.commands.name.${c.command}`)}
              {c.force ? ` · ${t("prs.commands.forced")}` : ""}
            </span>
            <Badge variant={COMMAND_VARIANT[c.status] ?? "default"} className="px-1.5 py-0">
              {t(`prs.commands.status.${c.status}`)}
            </Badge>
            {c.actor_name && (
              <span className="text-xs text-[var(--color-muted-foreground)]">
                {t("prs.commands.by", { name: c.actor_name })}
              </span>
            )}
            <span className="text-xs text-[var(--color-subtle-foreground)]"
              title={formatDateTime(c.created_at)}>
              {relativeTime(c.created_at, locale)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ReviewRunItem({ run, ordinal, defaultOpen }: {
  run: ReviewRunOut; ordinal: number; defaultOpen: boolean;
}) {
  const { t, locale } = useI18n();
  const [open, setOpen] = useState(defaultOpen);
  const status = run.status ?? run.verdict;
  const duration = runDurationMs(run);
  const statusText = t(`prs.review.${status}`) === `prs.review.${status}`
    ? status : t(`prs.review.${status}`);
  return (
    <li className={cn(
      "overflow-hidden rounded-lg border bg-[var(--color-card)] transition-[border-color,box-shadow] duration-200",
      open ? "border-[var(--color-border-strong)] shadow-[var(--shadow-sm)]" : "border-[var(--color-border)]",
    )}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center gap-x-2.5 gap-y-1 px-3 py-2.5 text-left text-sm transition-colors hover:bg-[var(--color-accent)]/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-ring)]"
      >
        <ChevronRightIcon aria-hidden className={cn(
          "h-4 w-4 shrink-0 text-[var(--color-muted-foreground)] transition-transform duration-200 ease-out-quint",
          open && "rotate-90",
        )} />
        <span className="font-semibold">{t("prs.runTitle", { n: ordinal })}</span>
        <StatusPill status={toRunStatus(status)} label={statusText} />
        <span className="text-xs tabular-nums text-[var(--color-muted-foreground)]">
          {t("prs.duration", { d: formatDuration(duration) })}
        </span>
        <span className="text-xs text-[var(--color-subtle-foreground)]" title={formatDateTime(run.started_at)}>
          {relativeTime(run.started_at, locale)}
        </span>
        {run.status_reason && (
          <span className="basis-full pl-6 text-xs leading-relaxed text-[var(--color-muted-foreground)]">
            {run.status_reason}
          </span>
        )}
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <m.div
            key="stages"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.26, ease: EASE }}
            className="overflow-hidden"
          >
            <div className="border-t border-[var(--color-border)] bg-[var(--color-muted)]/35 px-3 py-3.5 sm:px-4">
              {run.stages && run.stages.length > 0
                ? <StageTimeline stages={run.stages} />
                : <p className="text-sm text-[var(--color-muted-foreground)]">{t("prs.noStages")}</p>}
            </div>
          </m.div>
        )}
      </AnimatePresence>
    </li>
  );
}

const NODE_CLASS: Record<StageStatus, string> = {
  success: "border-[var(--color-status-success)]/40 bg-[var(--color-status-success-soft)] text-[var(--color-status-success)]",
  skipped: "border-[var(--color-border-strong)] bg-[var(--color-status-skipped-soft)] text-[var(--color-status-skipped)]",
  running: "border-[var(--color-status-running)]/40 bg-[var(--color-status-running-soft)] text-[var(--color-status-running)]",
  failed: "border-[var(--color-status-failed)]/45 bg-[var(--color-status-failed-soft)] text-[var(--color-status-failed)]",
};

/** The stage's mark on the rail: a check, a dash, a spinner or a cross in
 *  a ring of its status colour. A stage with no status yet is a hollow ring. */
function StageNode({ status }: { status: string | null | undefined }) {
  if (!status) {
    return (
      <span aria-hidden className="grid size-5 place-items-center rounded-full border border-dashed border-[var(--color-input)] bg-[var(--color-card)]" />
    );
  }
  const word = stageStatusWord(status);
  const Icon = word === "success" ? CheckIcon
    : word === "skipped" ? MinusIcon
      : word === "running" ? LoaderIcon : XIcon;
  return (
    <span aria-hidden className={cn("grid size-5 place-items-center rounded-full border", NODE_CLASS[word])}>
      <Icon strokeWidth={2.75} className={cn("size-3", word === "running" && "animate-spin motion-reduce:animate-none")} />
    </span>
  );
}

const STAGE_PILL: Record<StageStatus, RunStatus> = {
  success: "success", skipped: "skipped", running: "running", failed: "failed",
};

export function StageTimeline({ stages }: { stages: ReviewStage[] }) {
  const { t } = useI18n();
  const label = (s: ReviewStage) => {
    const l = stageLabel(s);
    if (!l.key) return s.name;
    const text = t(l.key, l.vars);
    return text === l.key ? s.name : text;
  };
  const metaLabel = (k: string) => {
    const text = t(`prs.meta.${k}`);
    return text === `prs.meta.${k}` ? k : text;
  };
  // The cascade adds up to at most ~0.3s however many stages there are.
  const step = Math.min(0.035, 0.3 / Math.max(1, stages.length));
  return (
    <ol className="relative">
      {stages.map((s, i) => {
        const word = stageStatusWord(s.status);
        const last = i === stages.length - 1;
        const meta = metaEntries(s.meta);
        return (
          <m.li
            key={`${s.key}-${i}`}
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.22, delay: i * step, ease: EASE }}
            className="relative grid grid-cols-[1.25rem_1fr] gap-x-3 pb-3.5 last:pb-0"
          >
            {/* The rail: a segment from under this node to the next one. */}
            {!last && (
              <span aria-hidden className="absolute left-[9.5px] top-5 bottom-0 w-px bg-[var(--color-input)]/55" />
            )}
            <StageNode status={s.status} />
            <div className="min-w-0 pt-px">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                <span className={cn("font-medium", word === "skipped" && "text-[var(--color-muted-foreground)]")}>
                  {label(s)}
                </span>
                {s.status && (
                  <StatusPill status={STAGE_PILL[word]} label={t(`prs.stageStatus.${word}`)}
                    className="px-1.5 text-[11px]" />
                )}
                <span className="ml-auto flex items-center gap-2 text-xs tabular-nums text-[var(--color-subtle-foreground)]">
                  <span>{formatDuration(s.duration_ms)}</span>
                  <span className="hidden sm:inline">{formatDateTime(s.started_at)}</span>
                </span>
              </div>
              {s.reason && (
                <p className="mt-0.5 text-xs leading-relaxed text-[var(--color-muted-foreground)]">{s.reason}</p>
              )}
              {meta.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {meta.map(([k, v]) => (
                    <span key={k}
                      className="inline-flex items-baseline gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-1.5 py-0.5 font-mono text-[11px] leading-4 text-[var(--color-muted-foreground)]">
                      <span>{metaLabel(k)}</span>
                      <span className="text-[var(--color-foreground)]">{v}</span>
                    </span>
                  ))}
                </div>
              )}
            </div>
          </m.li>
        );
      })}
    </ol>
  );
}
