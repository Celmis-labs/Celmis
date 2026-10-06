"use client";

/**
 * The strip over the issues list: how many suggestions the team takes, how
 * much is still open on the target branches, how much the resolver closed by
 * itself — and the button that asks it to look again now.
 */

import { RefreshCwIcon } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { issuesApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { Button } from "@/components/ui/button";

const DAYS = 30;

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-[8rem]">
      <div className="text-xs text-[var(--color-muted-foreground)]">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="text-xs text-[var(--color-muted-foreground)]">{hint}</div>}
    </div>
  );
}

export function IssuesBacklogSummary({ repo, canEdit }: { repo: string; canEdit: boolean }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const summary = useQuery({
    queryKey: ["issues-summary", repo, DAYS],
    queryFn: () => issuesApi.summary(token!, { repo: repo || undefined, days: DAYS }),
    enabled: !!token,
  });
  const recheck = useMutation({
    mutationFn: () => issuesApi.recheck(token!, repo || undefined),
    onSuccess: ({ queued }) => {
      toast.success(queued ? t("issues.recheckQueued", { n: queued }) : t("issues.recheckNone"));
      // The rechecks run in the background: look again after they had time.
      window.setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["issues"] });
        qc.invalidateQueries({ queryKey: ["issues-summary"] });
      }, 4000);
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const s = summary.data;
  const rate = s?.implementation_rate;
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] px-4 py-3">
      <div className="flex flex-wrap gap-x-8 gap-y-3">
        <Stat
          label={t("issues.summary.rate")}
          value={rate == null ? t("issues.summary.none") : `${Math.round(rate * 100)}%`}
          hint={t("issues.summary.rateHint", { days: DAYS })}
        />
        <Stat label={t("issues.summary.backlog")} value={String(s?.backlog_open ?? "—")} />
        <Stat
          label={t("issues.summary.auto")}
          value={String(s?.auto_resolved ?? "—")}
          hint={t("issues.summary.autoHint", { days: DAYS })}
        />
      </div>
      {canEdit && (
        <Button
          size="sm" variant="outline"
          disabled={recheck.isPending}
          onClick={() => recheck.mutate()}
        >
          <RefreshCwIcon aria-hidden className="h-4 w-4" />
          {t("issues.recheck")}
        </Button>
      )}
    </div>
  );
}
