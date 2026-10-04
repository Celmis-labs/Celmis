"use client";

/**
 * A pull request's reviews, each expandable into its stages — the Kodus-style
 * timeline: "Review 2 · Skipped · Duration 3m 18s · 3 days ago", and under it
 * every stage with a status pill, its duration, when it started and why.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2Icon, ChevronDownIcon, ChevronRightIcon, CircleDashedIcon,
  Loader2Icon, MinusCircleIcon, XCircleIcon,
} from "lucide-react";

import { pullRequestsApi, type ReviewRunOut } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import {
  formatDuration, metaEntries, relativeTime, runDurationMs, stageLabel,
  stagePillVariant, stageStatusWord, type ReviewStage,
} from "@/lib/review-stages";
import { useToken } from "@/lib/use-token";
import { Badge } from "@/components/ui/badge";
import { QueryState } from "@/components/ui/query-state";

/** A run's lifecycle word as a badge — the same map the PR row uses. */
export const RUN_VARIANT: Record<string, "success" | "warning" | "default" | "destructive" | "brand"> = {
  complete: "success",
  partial: "warning",
  skipped: "default",
  failed: "destructive",
  queued: "brand",
  running: "brand",
};

const ACTIVE = new Set(["queued", "running"]);

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
          <p className="py-2 text-xs text-[var(--color-muted-foreground)]">{t("prs.noRuns")}</p>
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

function ReviewRunItem({ run, ordinal, defaultOpen }: {
  run: ReviewRunOut; ordinal: number; defaultOpen: boolean;
}) {
  const { t, locale } = useI18n();
  const [open, setOpen] = useState(defaultOpen);
  const status = run.status ?? run.verdict;
  const duration = runDurationMs(run);
  return (
    <li className="rounded-md border border-[var(--color-border)] bg-[var(--color-card)]">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 text-left text-xs hover:bg-[var(--color-accent)]/40"
      >
        {open ? <ChevronDownIcon className="h-3.5 w-3.5 shrink-0" />
          : <ChevronRightIcon className="h-3.5 w-3.5 shrink-0" />}
        <span className="font-medium">{t("prs.runTitle", { n: ordinal })}</span>
        <Badge variant={RUN_VARIANT[status] ?? "default"}>
          {t(`prs.review.${status}`) === `prs.review.${status}` ? status : t(`prs.review.${status}`)}
        </Badge>
        <span className="text-[var(--color-muted-foreground)]">
          {t("prs.duration", { d: formatDuration(duration) })}
        </span>
        <span className="text-[var(--color-muted-foreground)]" title={formatDateTime(run.started_at)}>
          · {relativeTime(run.started_at, locale)}
        </span>
        {run.status_reason && (
          <span className="basis-full pl-5 text-[var(--color-muted-foreground)]">
            {run.status_reason}
          </span>
        )}
      </button>
      {open && (
        <div className="border-t border-[var(--color-border)] px-3 py-3">
          {run.stages && run.stages.length > 0
            ? <StageTimeline stages={run.stages} />
            : <p className="text-xs text-[var(--color-muted-foreground)]">{t("prs.noStages")}</p>}
        </div>
      )}
    </li>
  );
}

function StatusIcon({ status }: { status: string }) {
  const word = stageStatusWord(status);
  const cls = "h-4 w-4";
  if (word === "success") return <CheckCircle2Icon className={`${cls} text-[var(--color-success)]`} />;
  if (word === "skipped") return <MinusCircleIcon className={`${cls} text-[var(--color-muted-foreground)]`} />;
  if (word === "running") return <Loader2Icon className={`${cls} animate-spin text-[var(--color-brand)]`} />;
  return <XCircleIcon className={`${cls} text-[var(--color-destructive)]`} />;
}

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
  return (
    <ol className="relative ml-2 border-l border-[var(--color-border)]">
      {stages.map((s, i) => {
        const word = stageStatusWord(s.status);
        return (
          <li key={`${s.key}-${i}`} className="relative pb-3 pl-5 last:pb-0">
            <span className="absolute -left-2 top-0 rounded-full bg-[var(--color-card)]">
              {s.status ? <StatusIcon status={s.status} /> : <CircleDashedIcon className="h-4 w-4" />}
            </span>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
              <span className="font-medium">{label(s)}</span>
              <Badge variant={stagePillVariant(s.status)} className="text-[10px]">
                {t(`prs.stageStatus.${word}`)}
              </Badge>
              <span className="tabular-nums text-[var(--color-muted-foreground)]">
                {formatDuration(s.duration_ms)}
              </span>
              <span className="text-[var(--color-muted-foreground)]">
                {formatDateTime(s.started_at)}
              </span>
            </div>
            {s.reason && (
              <p className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">{s.reason}</p>
            )}
            {metaEntries(s.meta).length > 0 && (
              <div className="mt-1 flex flex-wrap gap-1">
                {metaEntries(s.meta).map(([k, v]) => (
                  <span key={k}
                    className="rounded border border-[var(--color-border)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--color-muted-foreground)]">
                    {metaLabel(k)}: {v}
                  </span>
                ))}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
