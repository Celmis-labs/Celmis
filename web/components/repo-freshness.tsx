"use client";

/**
 * Is this repository's index still the code that is on the branch?
 *
 * The list could say when a graph was built and never whether it was current,
 * so "indexed three days ago" meant either nobody had looked since or the
 * branch had not moved — and those are the two answers a person opens the
 * page to tell apart.
 *
 * FOUR STATES, NOT TWO. Up to date, behind, never checked, and *could not
 * check*. The last one is why `up_to_date` is a nullable boolean rather than
 * a flag: rendering "could not reach the remote" as "no new changes" answers
 * the question wrongly under a fresh timestamp, which is worse than leaving
 * it unanswered.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckIcon, RefreshCwIcon, TriangleAlertIcon } from "lucide-react";
import { toast } from "sonner";

import { api, type RepoOut } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { Button } from "@/components/ui/button";

/** "2 h ago" from an ISO string, or null when there is nothing to say. */
function ago(iso: string | null | undefined, t: (k: string, v?: Record<string, string | number>) => string): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 1) return t("freshness.justNow");
  if (mins < 60) return t("freshness.minutesAgo", { n: mins });
  const hours = Math.round(mins / 60);
  if (hours < 24) return t("freshness.hoursAgo", { n: hours });
  return t("freshness.daysAgo", { n: Math.round(hours / 24) });
}

export function RepoFreshness({ repo, token }: { repo: RepoOut; token: string | null }) {
  const t = useT();
  const qc = useQueryClient();

  const check = useMutation({
    mutationFn: async () =>
      api<{ state: string; reindex_queued: boolean; detail?: string | null }>(
        `/api/repos/${repo.slug}/check-freshness`, { method: "POST", token: token! }),
    onSuccess: (r) => {
      if (r.state === "behind") toast.success(t("freshness.toastBehind"));
      else if (r.state === "up_to_date") toast.info(t("freshness.toastUpToDate"));
      else if (r.state === "unreachable") toast.error(t("freshness.toastUnreachable"));
      else toast.info(t("freshness.toastNeverIndexed"));
      void qc.invalidateQueries({ queryKey: ["repos"] });
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : t("freshness.toastUnreachable")),
  });

  const checked = ago(repo.last_checked_at, t);
  const indexed = ago(repo.last_indexed_at, t);

  // Order matters: an error outranks a stale "up to date" from an earlier
  // check, because the newest thing we know is that we could not look.
  let label: string;
  let tone = "text-[var(--color-muted-foreground)]";
  let Icon = RefreshCwIcon;
  if (repo.last_check_error) {
    label = t("freshness.couldNotCheck");
    tone = "text-amber-600 dark:text-amber-500";
    Icon = TriangleAlertIcon;
  } else if (repo.up_to_date === true) {
    label = checked ? t("freshness.noChangesAt", { when: checked })
                    : t("freshness.noChanges");
    tone = "text-emerald-700 dark:text-emerald-500";
    Icon = CheckIcon;
  } else if (repo.up_to_date === false) {
    label = t("freshness.behind");
    tone = "text-amber-600 dark:text-amber-500";
    Icon = TriangleAlertIcon;
  } else {
    // null — never checked, or nothing recorded to compare against. Says so
    // rather than guessing, and still reports when the graph was built.
    label = indexed ? t("freshness.neverCheckedIndexed", { when: indexed })
                    : t("freshness.neverChecked");
  }

  return (
    <span className="flex flex-wrap items-center gap-1.5">
      <span className={`inline-flex items-center gap-1 ${tone}`} title={
        repo.last_check_error
          ? `${t("freshness.couldNotCheck")}: ${repo.last_check_error}`
          : (repo.last_remote_sha
              ? `${t("freshness.remoteIs")} ${repo.last_remote_sha.slice(0, 7)}`
              : undefined)
      }>
        <Icon className="h-3.5 w-3.5 shrink-0" />
        {label}
      </span>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="h-6 px-1.5"
        disabled={check.isPending || !token}
        onClick={() => check.mutate()}
        title={t("freshness.checkNowTitle")}
      >
        {check.isPending ? t("freshness.checking") : t("freshness.checkNow")}
      </Button>
    </span>
  );
}
