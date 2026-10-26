"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { HourglassIcon, RotateCwIcon } from "lucide-react";
import { toast } from "sonner";

import { jobsApi, type ReviewRunWaiting } from "@/lib/api";
import { canRetryNow, reasonSentence } from "@/lib/deferred-review";
import { formatDateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { useCanManageWorkspace } from "@/lib/use-workspace-role";
import { Button } from "@/components/ui/button";

/** Why a review is not running, since when, when the model is looked at next,
 *  and — for a workspace owner/admin — a button that looks right now. */
export function DeferredNotice({ waiting }: { waiting: ReviewRunWaiting }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const canManage = useCanManageWorkspace();
  const retry = useMutation({
    mutationFn: () => jobsApi.retry(token!, waiting.job_id),
    onSuccess: () => {
      toast.success(t("deferred.retryQueued"));
      qc.invalidateQueries({ queryKey: ["history"] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["pr-runs"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const reason = reasonSentence(waiting.reason);
  return (
    <div
      data-testid="deferred-notice"
      className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md bg-[var(--color-muted)] p-2.5 text-xs leading-relaxed text-[var(--color-muted-foreground)]"
    >
      <HourglassIcon aria-hidden className="h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0 flex-1 wrap-anywhere">
        <strong className="font-medium text-[var(--color-foreground)]">{t("deferred.title")}</strong>
        {reason ? ` ${reason}` : ""}
        {waiting.since ? ` ${t("deferred.since", { time: formatDateTime(waiting.since) })}` : ""}
        {waiting.next_check_at
          ? ` ${t("deferred.nextCheck", { time: formatDateTime(waiting.next_check_at) })}`
          : ""}
      </span>
      {canRetryNow(waiting, canManage) && (
        <Button type="button" size="xs" variant="outline" disabled={retry.isPending}
          onClick={() => retry.mutate()} title={t("deferred.retryNowHint")}>
          <RotateCwIcon className={`h-3.5 w-3.5 ${retry.isPending ? "animate-spin" : ""}`} />
          {t("deferred.retryNow")}
        </Button>
      )}
    </div>
  );
}
