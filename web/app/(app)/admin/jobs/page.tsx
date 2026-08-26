"use client";

/**
 * /admin/jobs — Postgres-backed job queue.
 *
 * Every durable async operation flows here: PR reviews (webhook + poller),
 * repo incremental index, vault builds, ownership rebuilds, cross-repo
 * materialize, qdrant re-embed. Dead rows can be retried; pending rows are
 * visible with next_run_at + attempts so operators know when a retry storm
 * is imminent.
 *
 * The whole-page global-admin gate is gone. This page is where a person
 * finds out whether their own repository is still indexing, and that wall
 * meant the answer was "ask somebody else". The API scopes rows to the
 * caller's active workspace instead (src/api/routers/jobs.py), so what
 * changed is which ROWS exist, not who is trusted:
 *
 *   read   any member — their own workspace's rows, and nothing else
 *   write  useCanManageWorkspace() — owner/admin of the active workspace
 *
 * Rows with no workspace (queue-wide maintenance) stay global-admin-only.
 * That is stated on the page rather than left as a silent omission: a
 * queue that hides rows without saying so is worse than one that refuses.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { toast } from "sonner";
import { RefreshCwIcon, RotateCwIcon, SquareIcon, Trash2Icon, ZapIcon } from "lucide-react";

import { jobsApi, type SyncJob } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { useCanManageWorkspace } from "@/lib/use-workspace-role";
import { formatDateTime } from "@/lib/format";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { QueryState } from "@/components/ui/query-state";

/** The API now returns the tenant on every row. Widened here rather than in
 *  lib/api.ts, which is being edited elsewhere; the field is optional so an
 *  older backend still type-checks. */
type ScopedJob = SyncJob & { workspace_id?: string | null };

const STATUS_TONES: Record<string, string> = {
  pending: "text-blue-600",
  running: "text-amber-600 font-semibold",
  completed: "text-emerald-600",
  failed: "text-orange-600",
  cancelled: "text-zinc-500",
  dead: "text-red-700 font-semibold",
};

const STATUS_KEYS: Record<string, string> = {
  pending: "admin.jobs.statusPending",
  running: "admin.jobs.statusRunning",
  completed: "admin.jobs.statusCompleted",
  failed: "admin.jobs.statusFailed",
  cancelled: "admin.jobs.statusCancelled",
  dead: "admin.jobs.statusDead",
};

export default function JobsPage() {
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  const { confirm, dialog } = useConfirm();
  const { data: session } = useSession();
  // Both hooks run unconditionally, above every branch — a hook below an
  // early return has taken this app down before.
  const canManage = useCanManageWorkspace();
  const isGlobalAdmin = Boolean(session?.isAdmin);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [kindFilter, setKindFilter] = useState<string>("");

  const stats = useQuery({
    queryKey: ["jobs", "stats"],
    queryFn: () => jobsApi.stats(token!),
    enabled: !!token,
    refetchInterval: 5000,
  });

  const list = useQuery({
    queryKey: ["jobs", statusFilter, kindFilter],
    queryFn: () => jobsApi.list(token!, {
      status: statusFilter || undefined,
      kind: kindFilter || undefined,
      limit: 200,
    }) as Promise<ScopedJob[]>,
    enabled: !!token,
    refetchInterval: 5000,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
  };

  const retry = useMutation({
    mutationFn: (id: string) => jobsApi.retry(token!, id),
    onSuccess: () => { toast.success(t("admin.jobs.toastRequeued")); invalidate(); },
    onError: (e: Error) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: (id: string) => jobsApi.remove(token!, id),
    onSuccess: () => { toast.success(t("admin.jobs.toastDeleted")); invalidate(); },
    onError: (e: Error) => toast.error(e.message),
  });

  const cancel = useMutation({
    mutationFn: (id: string) => jobsApi.cancel(token!, id),
    onSuccess: () => { toast.success(t("admin.jobs.toastCancelRequested")); invalidate(); },
    onError: (e: Error) => toast.error(e.message),
  });

  const totals = stats.data ?? {};
  const totalDead = totals.dead ?? 0;

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<ZapIcon className="h-6 w-6" />}
        title={t("admin.jobs.title")}
        description={t("admin.jobs.description")}
        tabs={<SectionTabs set="admin" />}
      />

      <div className="grid grid-cols-2 sm:grid-cols-6 gap-3">
        {(["pending", "running", "completed", "failed", "cancelled", "dead"] as const).map((s) => (
          <button
            key={s}
            className={`rounded border border-[var(--color-border)] p-3 text-left hover:bg-[var(--color-accent)] ${statusFilter === s ? "ring-2 ring-[var(--color-brand,#4f46e5)]" : ""}`}
            onClick={() => setStatusFilter(statusFilter === s ? "" : s)}
          >
            <div className="text-[10px] uppercase tracking-wide text-[var(--color-muted-foreground)]">{t(STATUS_KEYS[s])}</div>
            <div className={`text-xl ${STATUS_TONES[s] ?? ""}`}>
              {totals[s] ?? 0}
            </div>
          </button>
        ))}
      </div>

      {!isGlobalAdmin && (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("admin.jobs.scopeNotice")}
          {canManage === false && <> {t("admin.jobs.readOnlyNotice")}</>}
        </p>
      )}

      {totalDead > 0 && (
        <div className="rounded border border-red-600/40 bg-red-500/5 text-red-700 p-3 text-sm">
          <strong>{totalDead}</strong> {t("admin.jobs.deadBanner")}
        </div>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-2">
          <div>
            <CardTitle>{t("admin.jobs.cardTitle")}</CardTitle>
            <CardDescription>
              {t("admin.jobs.autoRefresh")}
              {statusFilter && <> {t("admin.jobs.filter")} <code>status={statusFilter}</code>.</>}
              {kindFilter && <> {t("admin.jobs.filter")} <code>kind={kindFilter}</code>.</>}
              {(statusFilter || kindFilter) && (
                <> <button className="underline text-xs" onClick={() => { setStatusFilter(""); setKindFilter(""); }}>{t("admin.jobs.clear")}</button></>
              )}
            </CardDescription>
          </div>
          <Button variant="ghost" size="sm" onClick={invalidate}>
            <RefreshCwIcon className="h-4 w-4 mr-1" /> {t("admin.jobs.refresh")}
          </Button>
        </CardHeader>
        <CardContent className="space-y-2">
          <QueryState
            query={list}
            empty={{ icon: ZapIcon, title: t("admin.jobs.noJobs") }}
          >
            {(jobs) => jobs.map((j) => (
              <JobRow key={j.id} j={j}
                canManage={canManage !== false}
                showWorkspace={isGlobalAdmin}
                onRetry={() => retry.mutate(j.id)}
                onCancel={() => cancel.mutate(j.id)}
                onDelete={async () => {
                  const ok = await confirm({
                    title: t("admin.jobs.confirmDelete", { id: j.id.slice(0,8) }),
                    confirmLabel: t("common.delete"),
                    danger: true,
                  });
                  if (ok) remove.mutate(j.id);
                }}
                onFilterKind={() => setKindFilter(j.kind)}
              />
            ))}
          </QueryState>
        </CardContent>
      </Card>
      {dialog}
    </PageShell>
  );
}


function JobRow({
  j, canManage, showWorkspace, onRetry, onCancel, onDelete, onFilterKind,
}: {
  j: ScopedJob;
  /** Owner/admin of the active workspace, or a global admin. Still
   *  undefined-tolerant: while the membership loads we draw the buttons
   *  rather than flash a refusal at somebody who turns out to be allowed —
   *  the API is the one that decides. */
  canManage: boolean;
  /** Only a global admin sees rows from more than one tenant, so only a
   *  global admin needs to be told which tenant a row came from. */
  showWorkspace: boolean;
  onRetry: () => void;
  onCancel: () => void;
  onDelete: () => void;
  onFilterKind: () => void;
}) {
  const t = useT();
  const canRetry = j.status === "dead" || j.status === "failed" || j.status === "cancelled";
  const payloadStr = useMemo(() => JSON.stringify(j.payload), [j.payload]);
  return (
    <div className="border border-[var(--color-border)] rounded p-2 text-xs">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={STATUS_TONES[j.status] ?? ""}>[{STATUS_KEYS[j.status] ? t(STATUS_KEYS[j.status]) : j.status}]</span>
            <button
              className="text-xs underline hover:no-underline"
              onClick={onFilterKind}
              title={t("admin.jobs.filterByKind")}
            >
              <code>{j.kind}</code>
            </button>
            <Badge variant="outline">{t("admin.jobs.attempts", { attempts: j.attempts, max: j.max_attempts })}</Badge>
            {showWorkspace && (
              <Badge variant="outline" className="font-mono text-[9px]"
                title={t("admin.jobs.workspaceTitle")}>
                {j.workspace_id || t("admin.jobs.noWorkspace")}
              </Badge>
            )}
            {j.dedup_key && <Badge variant="outline" className="font-mono text-[9px]">{j.dedup_key.slice(0, 60)}</Badge>}
            <span className="text-[var(--color-muted-foreground)] ml-auto">
              {j.status === "pending" ? t("admin.jobs.nextRun", { time: formatDateTime(j.next_run_at) }) : ""}
              {j.status === "running" && j.locked_by ? t("admin.jobs.lockedBy", { who: j.locked_by }) : ""}
              {j.status === "completed" && j.finished_at ? t("admin.jobs.done", { time: formatDateTime(j.finished_at) }) : ""}
            </span>
          </div>
          <div className="mt-1 font-mono truncate text-[var(--color-muted-foreground)]">
            {payloadStr.slice(0, 200)}{payloadStr.length > 200 ? "…" : ""}
          </div>
          {j.last_error && (
            <details className="mt-1">
              <summary className="cursor-pointer text-red-600">{t("admin.jobs.errorSummary")}</summary>
              <pre className="mt-1 bg-[var(--color-secondary)] rounded p-2 whitespace-pre-wrap overflow-x-auto">
                {j.last_error.slice(0, 2000)}
              </pre>
            </details>
          )}
        </div>
        {canManage && (
          <div className="flex gap-1 shrink-0">
            {j.status === "running" && (
              <Button variant="ghost" size="sm" onClick={onCancel}
                title={t("admin.jobs.cancelTitle")}>
                <SquareIcon className="h-3.5 w-3.5 mr-1" /> {t("admin.jobs.cancel")}
              </Button>
            )}
            {canRetry && (
              <Button variant="ghost" size="sm" onClick={onRetry}>
                <RotateCwIcon className="h-3.5 w-3.5 mr-1" /> {t("admin.jobs.retry")}
              </Button>
            )}
            <Button variant="ghost" size="icon" onClick={onDelete}>
              <Trash2Icon className="h-4 w-4 text-red-600" />
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
