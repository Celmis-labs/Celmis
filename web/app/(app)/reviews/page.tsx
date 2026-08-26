"use client";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ChevronDownIcon, ExternalLinkIcon, GitPullRequestIcon, HelpCircleIcon,
  RefreshCwIcon, SlidersHorizontalIcon, SparklesIcon, WrenchIcon,
} from "lucide-react";
import { useSession } from "next-auth/react";
import {
  api, applyFixApi, findingsApi,
  feedbackApi, workspacesApi,
  type ApplyFixIn, type FindingOut, type FindingsPayload, type PullRequestSummary,
  type RepoOut, type ReviewRunOut,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { DriftPanel } from "@/components/drift-panel";
import {
  ParameterAdjustmentsPanel, adjustmentsCount,
} from "@/components/parameter-adjustments";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { WebhookSetup } from "@/components/webhook-setup";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Callout } from "@/components/ui/callout";
import { EmptyState } from "@/components/ui/empty-state";
import { QueryState } from "@/components/ui/query-state";
import { Switch } from "@/components/ui/switch";
import { Select } from "@/components/ui/select";
import { WorkspaceBadge } from "@/components/workspace-badge";
import { useState } from "react";

// Apply-fix currently supports GitHub only — the "(n/a)" providers were
// noise in the select, so they are gone until the backend supports them.
const PROVIDER_OPTIONS = [
  { value: "github", label: "github" },
];

export default function ReviewsPage() {
  const token = useToken();
  const t = useT();
  const history = useQuery({
    queryKey: ["history", 50],
    queryFn: () => api<ReviewRunOut[]>("/api/reviews/history?limit=50", { token }),
    enabled: !!token,
    refetchInterval: 10_000, // pick up background poller results
  });
  // Same gate as Settings → LLM (owner/admin of the active workspace, or a
  // global admin) and the same cache key, so the two pages share one fetch.
  // Gated because a re-run spends the workspace's LLM budget: the button is
  // offered only to the people who can see and change that budget.
  const { data: session } = useSession();
  const wsMe = useQuery({
    queryKey: ["workspaces-me"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token,
  });
  const activeRole =
    wsMe.data?.workspaces.find((w) => w.id === wsMe.data?.active_id)?.role;
  const isAdmin =
    Boolean(session?.isAdmin) || activeRole === "owner" || activeRole === "admin";

  return (
    <PageShell width="wide">
      <PageHeader
        title={t("reviews.title")}
        badge={<WorkspaceBadge />}
        description={t("reviews.subtitle")}
        tabs={<SectionTabs set="review" />}
      />

      {/* Phone-first order: firing a review is what you open this page for, so
          the trigger leads and the 50-run history follows. On desktop the two
          columns swap back to history-left / panels-right. */}
      <div className="grid gap-6 lg:grid-cols-[1fr_360px]">
        <div className="flex flex-col gap-6 lg:order-2">
          <ManualTrigger />
          <AutoReviewPanel />
          {/* Directly under the toggle, because the toggle alone does nothing:
              a repo marked for auto-review still needs the provider told where
              to POST and what secret to sign with. Putting the instructions
              anywhere else is how the feature came to look broken. */}
          <WebhookSetup />
          <ApplyFixPanel />
        </div>

        <Card className="lg:order-1">
          <CardHeader>
            <CardTitle>{t("reviews.historyTitle")}</CardTitle>
            <CardDescription>
              {t("reviews.historyDescription")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <QueryState
              query={history}
              empty={{ icon: GitPullRequestIcon, title: t("reviews.noRuns") }}
            >
              {(runs) => (
                <div className="flex flex-col gap-2">
                  {runs.map((r, idx) => (
                    <RunRow key={r.id || `${r.pr_ref}-${idx}`} run={r} isAdmin={isAdmin} />
                  ))}
                </div>
              )}
            </QueryState>
          </CardContent>
        </Card>
      </div>
    </PageShell>
  );
}


function AutoReviewPanel() {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });

  const toggle = useMutation({
    mutationFn: ({ repo, enabled }: { repo: RepoOut; enabled: boolean }) =>
      api<RepoOut>(`/api/repos/${repo.slug}/auto-review`, {
        method: "PATCH",
        token,
        json: { enabled, mode: repo.provider === "bitbucket" ? "manual" : "polling" },
      }),
    onSuccess: (_d, { enabled }) => {
      toast.success(t(enabled ? "reviews.autoReviewEnabled" : "reviews.autoReviewDisabled"));
      qc.invalidateQueries({ queryKey: ["repos"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("reviews.autoReviewTitle")}</CardTitle>
        <CardDescription>{t("reviews.autoReviewDescription")}</CardDescription>
      </CardHeader>
      <CardContent>
        {(repos.data?.length ?? 0) === 0 ? (
          <div className="text-sm text-[var(--color-muted-foreground)] py-4 text-center">
            {t("reviews.autoReviewEmpty")}
          </div>
        ) : (
          <ul className="flex flex-col gap-2">
            {(repos.data ?? []).map((repo) => (
              <li key={repo.slug} className="flex min-h-11 items-center justify-between gap-3 sm:min-h-0">
                <div className="min-w-0">
                  <div className="text-sm font-medium truncate">{repo.full_name}</div>
                  <div className="text-xs text-[var(--color-muted-foreground)]">
                    {t(repo.provider === "bitbucket" ? "reviews.manualMode" : "reviews.pollingMode")}
                  </div>
                </div>
                {/* The pill is 20px, so ::after carries the 44px tap target
                    without resizing it. The row is min-h-11 to match, so two
                    rows' tap areas never touch — a stray tap must not arm
                    auto-review on the neighbouring repo. */}
                <Switch
                  checked={repo.auto_review_enabled}
                  disabled={toggle.isPending}
                  onCheckedChange={(v) => toggle.mutate({ repo, enabled: v })}
                  aria-label={t("reviews.autoReviewFor", { repo: repo.full_name || repo.slug })}
                 
                />
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}


function ApplyFixPanel() {
  const token = useToken();
  const t = useT();
  const [helpOpen, setHelpOpen] = useState(false);
  // Eleven raw fields read like a bug report, not a feature — collapsed by
  // default so the panel is an escape hatch, not the first thing you see.
  const [open, setOpen] = useState(false);
  const [payload, setPayload] = useState({
    run_id: "",
    provider: "github" as "github" | "gitlab" | "bitbucket",
    repo: "",
    pr_number: "",
    head_ref: "",
    head_sha: "",
    file_path: "",
    line_start: "",
    line_end: "",
    replacement: "",
    finding_id: "",
    commit_message: "",
  });

  const submit = useMutation({
    mutationFn: () => {
      const body = {
        provider: payload.provider,
        repo: payload.repo.trim(),
        pr_number: Number(payload.pr_number),
        head_ref: payload.head_ref.trim(),
        head_sha: payload.head_sha.trim(),
        file_path: payload.file_path.trim(),
        line_start: Number(payload.line_start),
        line_end: Number(payload.line_end),
        replacement: payload.replacement,
        finding_id: payload.finding_id || undefined,
        commit_message: payload.commit_message || undefined,
      };
      return api<{ ok: boolean; commit_url: string | null; branch: string | null;
                  detail: string; check_state?: string; check_reason?: string }>(
        `/api/reviews/${encodeURIComponent(payload.run_id || "manual")}/apply-fix`,
        { method: "POST", token, json: body },
      );
    },
    onSuccess: (r) => {
      toast.success(t("reviews.committedTo", { branch: r.branch ?? "" }), { description: r.commit_url ?? undefined });
    },
    onError: (e: Error) => toast.error(t("reviews.applyFailed", { message: e.message })),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-base">
              <WrenchIcon className="h-4 w-4 shrink-0" />
              {t("reviews.afAdvancedTitle")}
              {/* Negative margins keep the icon where it looks right while the
                  tap target grows to 44px around it. */}
              <Button type="button" variant="ghost" size="icon"
                onClick={() => setHelpOpen(true)}
                aria-label={t("reviews.afHelpTitle")}
                className="-my-3 -mr-3 size-11 shrink-0 text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]">
                <HelpCircleIcon className="h-4 w-4" />
              </Button>
            </CardTitle>
            <CardDescription className="mt-1">{t("reviews.afWhen")}</CardDescription>
          </div>
          <Button
            variant="ghost"
            size="sm"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="min-h-11 shrink-0 sm:min-h-0"
          >
            {open ? t("reviews.afHide") : t("reviews.afShow")}
            <ChevronDownIcon className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
          </Button>
        </div>
      </CardHeader>
      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("reviews.afHelpTitle")}</DialogTitle>
            <DialogDescription>{t("reviews.afHelpIntro")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <Callout tone="info">{t("reviews.afWhen")}</Callout>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("reviews.afHelpHow")}</p>
            <div className="space-y-2 text-xs">
              {([
                ["run_id", "afHelpRunId"], ["provider / repo / pr_number", "afHelpRepo"],
                ["head_ref / head_sha", "afHelpHead"], ["file_path", "afHelpFile"],
                ["line_start / line_end", "afHelpLines"], ["replacement", "afHelpReplacement"],
              ] as const).map(([field, key]) => (
                <div key={field}>
                  <code className="text-[11px]">{field}</code>
                  <span className="block text-[var(--color-muted-foreground)]">{t(`reviews.${key}`)}</span>
                </div>
              ))}
            </div>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("reviews.afHelpTip")}</p>
          </div>
        </DialogContent>
      </Dialog>
      {open && (
      <CardContent className="space-y-3 text-sm">
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("reviews.applyFixDescBefore")}
          <code>celmis-fix/&lt;pr&gt;-…</code>
          {t("reviews.applyFixDescAfter")}
        </p>
        <fieldset className="space-y-2 rounded-md border border-[var(--color-border)] p-3">
          <legend className="px-1 text-xs font-medium uppercase tracking-wide text-[var(--color-muted-foreground)]">
            {t("reviews.af.groupTarget")}
          </legend>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <Label>{t("reviews.provider")}</Label>
              <Select
                className="w-full h-8 px-2 text-sm"
                value={payload.provider}
                onChange={(v) => setPayload({ ...payload, provider: v as "github" | "gitlab" | "bitbucket" })}
                options={PROVIDER_OPTIONS}
              />
            </div>
            <div>
              <Label>{t("reviews.runIdLabel")}</Label>
              <Input value={payload.run_id}
                     onChange={(e) => setPayload({ ...payload, run_id: e.target.value })}
                     placeholder={t("reviews.runIdPlaceholder")} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div><Label>{t("reviews.repoLabel")}</Label><Input value={payload.repo}
                  onChange={(e) => setPayload({ ...payload, repo: e.target.value })} /></div>
            <div><Label>{t("reviews.prNumberLabel")}</Label><Input value={payload.pr_number}
                  onChange={(e) => setPayload({ ...payload, pr_number: e.target.value })} /></div>
            <div><Label>{t("reviews.af.headRef")}</Label><Input value={payload.head_ref}
                  onChange={(e) => setPayload({ ...payload, head_ref: e.target.value })} /></div>
            <div><Label>{t("reviews.af.headSha")}</Label><Input value={payload.head_sha}
                  onChange={(e) => setPayload({ ...payload, head_sha: e.target.value })} /></div>
          </div>
          <div><Label>{t("reviews.af.filePath")}</Label><Input value={payload.file_path}
               onChange={(e) => setPayload({ ...payload, file_path: e.target.value })} /></div>
        </fieldset>
        <fieldset className="space-y-2 rounded-md border border-[var(--color-border)] p-3">
          <legend className="px-1 text-xs font-medium uppercase tracking-wide text-[var(--color-muted-foreground)]">
            {t("reviews.af.groupChange")}
          </legend>
          <div className="grid grid-cols-2 gap-2">
            <div><Label>{t("reviews.af.lineStart")}</Label><Input value={payload.line_start}
                  onChange={(e) => setPayload({ ...payload, line_start: e.target.value })} /></div>
            <div><Label>{t("reviews.af.lineEnd")}</Label><Input value={payload.line_end}
                  onChange={(e) => setPayload({ ...payload, line_end: e.target.value })} /></div>
          </div>
          <div>
            <Label>{t("reviews.replacementLabel")}</Label>
            <textarea
              className="w-full rounded border border-[var(--color-border)] bg-transparent px-2 py-1 text-sm font-mono"
              rows={4}
              value={payload.replacement}
              onChange={(e) => setPayload({ ...payload, replacement: e.target.value })}
            />
          </div>
        </fieldset>
        <Button
          onClick={() => submit.mutate()}
          disabled={submit.isPending || !payload.repo || !payload.file_path || !payload.head_sha}
          size="sm"
        >
          {submit.isPending ? t("reviews.applying") : t("reviews.applyFixButton")}
        </Button>
      </CardContent>
      )}
    </Card>
  );
}

// Lifecycle → badge tone + label key. The label goes through i18n (unlike
// the raw verdict, which is provider vocabulary) because "partial" and
// "skipped" are claims about what the reviewer did, not quotes from it.
const STATUS_BADGES: Record<
  string,
  { variant: "success" | "warning" | "destructive" | "outline" | "default"; key: string }
> = {
  queued: { variant: "outline", key: "reviews.status.queued" },
  running: { variant: "default", key: "reviews.status.running" },
  complete: { variant: "success", key: "reviews.status.complete" },
  partial: { variant: "warning", key: "reviews.status.partial" },
  failed: { variant: "destructive", key: "reviews.status.failed" },
  skipped: { variant: "outline", key: "reviews.status.skipped" },
};

function RunRow({ run, isAdmin }: { run: ReviewRunOut; isAdmin: boolean }) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [showFindings, setShowFindings] = useState(false);
  // What Celmis changed on its own while this run was in flight — a clamped
  // ceiling, a reasoning level or temperature the provider refused, a
  // fallback model that took an agent over. Zero for a run that predates the
  // list, which is "nobody wrote it down" and gets no badge rather than a
  // false all-clear. Closed by default: the badge is the headline, the table
  // is the detail, and four out of five rows never need it open.
  const [showAdjustments, setShowAdjustments] = useState(false);
  const adjCount = adjustmentsCount(run);
  // 'error' is what the queue's except-arm writes; same claim as 'failed'.
  const status = run.status === "error" ? "failed" : (run.status ?? "");
  const statusBadge = STATUS_BADGES[status];
  // Legacy skips carry verdict='skipped' with status='complete' — the reason
  // line below must cover them too, or old rows lose their explanation.
  const isSkipped = status === "skipped" || run.verdict === "skipped";
  const failedAgents = (run.agents_failed ?? []).join(", ");
  const cleanup = run.cleanup ?? null;
  // Whole-review re-run, same call the manual trigger makes. post_comments
  // stays false: a re-run from history is an inspection, and posting is a
  // decision the trigger panel exists to make explicitly.
  const rerun = useMutation({
    mutationFn: () =>
      api<ReviewRunOut>("/api/reviews/trigger", {
        method: "POST", token,
        json: { pr_ref: run.pr_ref, post_comments: false },
      }),
    onSuccess: (r) => {
      toast.success(t("reviews.rerunQueued", { id: r.id.slice(0, 8) }));
      // The queued row must appear now, not on the next 10s poll.
      qc.invalidateQueries({ queryKey: ["history"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const verdictTone =
    run.verdict === "approve"
      ? "success"
      : run.verdict === "changes" || run.verdict === "request_changes"
        ? "destructive"
        : run.verdict === "comment"
          ? "default"
          : "outline";
  return (
    <div className="rounded-lg border border-[var(--color-border)] p-3 hover:bg-[var(--color-accent)]/30">
      <div className="flex flex-wrap items-start justify-between gap-2 sm:flex-nowrap sm:items-center sm:gap-3">
        <div className="w-full min-w-0 sm:w-auto sm:flex-1">
          {/* Refs are long and unbreakable — wrap them on a phone rather than
              letting the row set a min-content width the viewport can't hold. */}
          <code className="block wrap-anywhere text-xs font-mono text-[var(--color-foreground)] sm:truncate">
            {run.pr_ref || t("reviews.unknownRef")}
          </code>
          <div className="text-xs text-[var(--color-muted-foreground)] mt-0.5">
            {formatDateTime(run.started_at)}
            {run.elapsed_seconds ? ` · ${Math.round(run.elapsed_seconds)}s` : ""}
            {run.posted ? ` · ${t("reviews.posted")}` : ""}
          </div>
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {run.cross_repo_callers > 0 && (
            <Badge variant="brand" title={t("reviews.crossRepoTitle")}>
              {t("reviews.crBadge", { count: run.cross_repo_callers })}
            </Badge>
          )}
          {statusBadge ? (
            <>
              <Badge
                variant={statusBadge.variant}
                title={failedAgents ? t("reviews.agentsFailed", { agents: failedAgents }) : undefined}
              >
                {t(statusBadge.key)}
              </Badge>
              {/* The verdict is only a claim a run that actually reviewed
                  something can make — for queued/running/failed/skipped the
                  serialiser folds status into `verdict`, and repeating it
                  next to the status badge would just say the word twice. */}
              {(status === "complete" || status === "partial")
                && run.verdict && run.verdict !== "pending" && (
                <Badge variant={verdictTone}>{run.verdict.toUpperCase()}</Badge>
              )}
            </>
          ) : (
            <Badge variant={verdictTone}>{run.verdict.toUpperCase()}</Badge>
          )}
          {/* The self-healing the runtime does is RIGHT in the moment and
              invisible afterwards: a review that ran with its reasoning
              dropped looks exactly like one that ran as configured, only
              worse. The badge is what makes it visible at the row; the
              table it opens says which knob to turn. A button, not a
              decorative badge, so it is reachable from the keyboard. */}
          {adjCount > 0 && (
            <button
              type="button"
              onClick={() => setShowAdjustments((v) => !v)}
              aria-expanded={showAdjustments}
              title={t("reviews.adjustmentsHint")}
              className="rounded-md focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand)]"
            >
              <Badge variant="warning" className="cursor-pointer gap-1">
                <SlidersHorizontalIcon className="h-3 w-3" />
                {adjCount === 1
                  ? t("reviews.adjustmentsBadgeOne")
                  : t("reviews.adjustmentsBadge", { count: adjCount })}
              </Badge>
            </button>
          )}
          <FindingsBreakdown run={run} />
          {isAdmin && run.pr_ref && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => rerun.mutate()}
              // A run still in flight is already the re-run — queueing a
              // second copy from its own row only doubles the bill.
              disabled={rerun.isPending || status === "queued" || status === "running"}
              title={t("reviews.rerunReview")}
              className="h-7 gap-1 px-2 text-xs"
            >
              <RefreshCwIcon className={`h-3 w-3 ${rerun.isPending ? "animate-spin" : ""}`} />
              {rerun.isPending ? t("reviews.queueing") : t("reviews.rerun")}
            </Button>
          )}
        </div>
      </div>
      {/* Named, not just counted. "partial" alone forces the reader back to
          the PR comment to learn WHICH stage is missing — the roster is
          persisted exactly so this row can still say it was security. */}
      {(status === "partial" || status === "failed") && failedAgents && (
        <div className="mt-1 text-xs text-[var(--color-warning)] wrap-anywhere">
          {t("reviews.agentsFailed", { agents: failedAgents })}
        </div>
      )}
      {showAdjustments && adjCount > 0 && <ParameterAdjustmentsPanel run={run} />}
      {cleanup && ((cleanup.deleted ?? 0) + (cleanup.failed ?? 0)
        + (cleanup.kept_threaded ?? 0) > 0 || cleanup.complete === false) && (
        <div className="mt-1 text-xs text-[var(--color-muted-foreground)]" title={t("reviews.cleanupHint")}>
          {[
            t("reviews.cleanupDeleted", { count: cleanup.deleted ?? 0 }),
            (cleanup.kept_threaded ?? 0) > 0
              ? t("reviews.cleanupKept", { count: cleanup.kept_threaded ?? 0 })
              : null,
            (cleanup.failed ?? 0) > 0
              ? t("reviews.cleanupFailed", { count: cleanup.failed ?? 0 })
              : null,
          ].filter(Boolean).join(" · ")}
          {/* `complete: false` means duplicates may still be on the PR — the
              providers report it so a half-done cleanup cannot pass for a
              finished one, and hiding it here would undo that. */}
          {cleanup.complete === false && (
            <span className="block text-[var(--color-warning)]">
              {t("reviews.cleanupIncomplete")}
            </span>
          )}
        </div>
      )}
      {isSkipped && run.summary ? (
        // The WHY, in the open. A skipped run's summary is its whole story
        // ("draft", "all files filtered", "agents disabled") — burying it
        // behind the Summary toggle made "skipped" look like a verdict when
        // it is an explanation.
        <div className="mt-2 rounded bg-[var(--color-secondary)] p-2 text-xs whitespace-pre-wrap wrap-anywhere text-[var(--color-muted-foreground)]">
          {run.summary}
        </div>
      ) : run.summary ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]">
            {t("reviews.summaryToggle")}
          </summary>
          <pre className="mt-2 text-xs whitespace-pre-wrap wrap-anywhere text-[var(--color-muted-foreground)] bg-[var(--color-secondary)] rounded p-2">
            {run.summary}
          </pre>
        </details>
      ) : null}
      {/* Drift is counted separately on purpose. `findings_count` is the
          MODEL's findings; a run that produced none of those can still have
          caught a constant left behind in a sibling repository — and that is
          the case the panel below was written for, in as many words. Gating
          on findings alone made that code unreachable, so the one result
          that needed no interpretation was invisible exactly when it was the
          only result there was. */}
      {((run.findings_count ?? 0) > 0 || (run.drift_hits ?? 0) > 0) && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowFindings((v) => !v)}
            className="text-xs underline text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]"
          >
            {showFindings
              ? t("reviews.hideFindings", { count: run.findings_count ?? 0 })
              : t("reviews.showFindings", { count: run.findings_count ?? 0 })}
          </button>
          {showFindings && <FindingsPanel runId={run.id} />}
        </div>
      )}
    </div>
  );
}


function FindingsPanel({ runId }: { runId: string }) {
  const token = useToken();
  const t = useT();
  const [pages, setPages] = useState(1);
  const pageSize = 50;
  const [showDiff, setShowDiff] = useState(false);
  const q = useQuery({
    queryKey: ["findings", runId, pages],
    queryFn: () => findingsApi.list(token!, runId, { limit: pageSize * pages, offset: 0 }),
    enabled: !!token,
  });
  if (q.isLoading) return <div className="text-xs mt-2">{t("reviews.loadingFindings")}</div>;
  if (q.error) return <div className="text-xs mt-2 text-red-600">{(q.error as Error).message}</div>;
  const p = q.data;
  if (!p) return null;
  if (p.legacy) {
    // The re-run lived here once — buried two clicks deep in the one panel a
    // legacy run happens to open. Every row header carries the (admin-gated)
    // re-run button now, so this is just the notice.
    return (
      <div className="text-xs mt-2 text-[var(--color-muted-foreground)]">
        {t("reviews.legacyFindings")}
      </div>
    );
  }
  if (!p.findings.length) {
    // Drift stands on its own. A review that produced no model findings can
    // still have caught a constant left behind in a sibling repository, and
    // rendering "no findings" over it would hide the one result that needed
    // no interpretation.
    return (
      <div className="mt-2 space-y-2">
        <DriftPanel drift={p.drift} />
        {!p.drift?.hits.length && (
          <div className="text-xs">{t("reviews.noFindings")}</div>
        )}
      </div>
    );
  }
  return (
    <div className="mt-2 space-y-1">
      {/* ABOVE the model's findings and outside their list. Not styling: a
          grep result and a judgement are different kinds of claim, and one
          list behind a badge invites equal trust in both. */}
      <DriftPanel drift={p.drift} />
      <div className="flex items-center gap-2 text-xs">
        <span className="text-[var(--color-muted-foreground)]">
          {t("reviews.findingsCount", { shown: p.findings.length, total: p.total })}
        </span>
        <button type="button" className="underline"
                onClick={() => setShowDiff((v) => !v)}>
          {showDiff ? t("reviews.hideDiff") : t("reviews.showDiff")}
        </button>
      </div>
      {showDiff && <DiffPanel runId={runId} findings={p.findings} />}
      {p.findings.map((f) => (
        <FindingRow key={f.id} f={f} runId={runId} pr={p.pr} />
      ))}
      {p.findings.length < p.total && (
        <button
          type="button"
          className="text-xs underline"
          onClick={() => setPages(pages + 1)}
        >
          {t("reviews.loadMore", { count: p.total - p.findings.length })}
        </button>
      )}
    </div>
  );
}


function DiffPanel({ runId, findings }: { runId: string; findings: FindingOut[] }) {
  const token = useToken();
  const t = useT();
  const q = useQuery({
    queryKey: ["diff", runId],
    queryFn: () => findingsApi.diff(token!, runId),
    enabled: !!token,
  });
  if (q.isLoading) return <div className="text-xs">{t("reviews.loadingDiff")}</div>;
  const d = q.data;
  if (!d?.available) {
    return (
      <div className="text-xs text-[var(--color-muted-foreground)]">
        {t("reviews.diffNotStored")}
      </div>
    );
  }
  // Marker map: file:line → severity for inline finding highlights.
  const markers = new Map<string, string>();
  for (const f of findings) markers.set(`${f.file_path}:${f.line}`, f.severity);

  const lines = d.diff.split("\n");
  let currentFile = "";
  let newLineNo = 0;
  return (
    <div className="max-h-96 overflow-auto rounded border border-[var(--color-border)] bg-[var(--color-secondary)] font-mono text-[11px] leading-4">
      {lines.map((ln, i) => {
        let cls = "";
        let markable = false; // only added / context lines carry findings
        if (ln.startsWith("+++")) {
          // New-file header. "+++ b/path" → path; "+++ /dev/null" (delete)
          // → no current file. Never a content line.
          const rest = ln.slice(4).trim();
          currentFile = rest === "/dev/null" ? "" : rest.replace(/^b\//, "");
          cls = "bg-[var(--color-accent)] font-semibold";
        } else if (ln.startsWith("---") || ln.startsWith("diff ") || ln.startsWith("index ") || ln.startsWith("\\")) {
          // Old-file header / git metadata / "\ No newline" — skip entirely.
          cls = "text-[var(--color-muted-foreground)]";
        } else if (ln.startsWith("@@")) {
          const m = ln.match(/\+(\d+)/);
          if (m) newLineNo = parseInt(m[1], 10) - 1;
          cls = "text-blue-600";
        } else if (ln.startsWith("+")) {
          newLineNo += 1;
          markable = true;
          cls = "bg-emerald-500/10 text-emerald-700";
        } else if (ln.startsWith("-")) {
          // Removed line — belongs to the OLD file, has no new-file line
          // number, so it must not carry a finding marker.
          cls = "bg-red-500/10 text-red-700";
        } else {
          // Context line.
          newLineNo += 1;
          markable = true;
        }
        const sev = markable ? markers.get(`${currentFile}:${newLineNo}`) : undefined;
        return (
          <div key={i}
               className={`px-2 whitespace-pre ${cls} ${sev ? "outline outline-1 outline-amber-500" : ""}`}
               title={sev ? t("reviews.findingMarker", { severity: sev }) : undefined}>
            {ln || " "}
          </div>
        );
      })}
    </div>
  );
}



/** Stable client-side identity for a finding.
 *
 * Findings live inside a JSON blob and get reordered between re-runs, so an
 * array index is useless as a key. We derive one from the fields that actually
 * pin a finding down. The API stores whatever key we send and returns it back,
 * so self-consistency here is what matters — it does not need to equal the
 * server-side `finding_key()` helper. */
function findingKey(f: FindingOut): string {
  const basis = `${f.rule_id || ""}|${f.file_path}|${f.line}|${f.title.trim().slice(0, 120)}`;
  return btoa(unescape(encodeURIComponent(basis))).replace(/[^a-zA-Z0-9]/g, "").slice(0, 20);
}

function FindingRow({
  f, runId, pr,
}: {
  f: FindingOut;
  runId: string;
  pr: FindingsPayload["pr"];
}) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  // Feedback closes the review loop — without it nobody learns which agent
  // produces noise. Keyed by a stable hash so it survives re-runs.
  const fkey = findingKey(f);
  const feedback = useQuery({
    queryKey: ["feedback", runId],
    queryFn: () => feedbackApi.forRun(token!, runId),
    enabled: !!token,
  });
  const verdict = feedback.data?.find((x) => x.finding_key === fkey)?.state ?? null;
  const setVerdict = useMutation({
    mutationFn: (state: "accepted" | "dismissed") =>
      feedbackApi.set(token!, runId, {
        finding_key: fkey, state,
        reason: state === "dismissed" ? "false_positive" : "",
        agent: f.agent, severity: f.severity, repo_slug: pr.repo ?? null,
      }),
    onSuccess: (_d, state) => {
      qc.invalidateQueries({ queryKey: ["feedback", runId] });
      toast.success(state === "dismissed" ? t("reviews.fbDismissed") : t("reviews.fbAccepted"));
    },
    onError: (e) => toast.error((e as Error).message),
  });
  const sevColor = {
    critical: "text-red-700 font-semibold",
    error: "text-orange-600 font-semibold",
    warning: "text-amber-600",
    info: "text-[var(--color-muted-foreground)]",
  }[f.severity] || "text-[var(--color-muted-foreground)]";

  const canApply = Boolean(
    f.suggestion && pr.provider === "github" && pr.repo && pr.number
    && pr.head_sha && pr.head_ref,
  );

  const apply = async () => {
    if (!canApply) return;
    setBusy(true);
    try {
      const payload: ApplyFixIn = {
        provider: pr.provider as "github",
        repo: pr.repo!,
        pr_number: pr.number!,
        head_ref: pr.head_ref!,
        head_sha: pr.head_sha!,
        file_path: f.file_path,
        line_start: f.line,
        line_end: f.line,
        replacement: f.suggestion || "",
        finding_id: f.id,
        commit_message: `Celmis: ${f.title.slice(0, 100)}`,
      };
      const r = await applyFixApi.apply(token!, runId, payload);
      // "still_fires" means the patch landed and the finding did NOT go away.
      // Reporting that as a plain success is what this whole change exists to
      // stop — the product used to say "committed" and nothing else.
      if (r.check_state === "still_fires") {
        toast.warning(
          `Committed to ${r.branch ?? ""}, but the finding still matches — review before merging.`,
          { description: r.commit_url ?? undefined },
        );
      } else {
        toast.success(t("reviews.committedTo", { branch: r.branch ?? "" }), {
          description: r.check_state === "applied_unchecked"
            ? `${r.commit_url ?? ""} · could not re-check this file type`
            : r.commit_url ?? undefined,
        });
      }
    } catch (e) {
      toast.error(t("reviews.applyFailed", { message: (e as Error).message }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded border border-[var(--color-border)] p-2 text-xs">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="wrap-anywhere">
            <span className={sevColor}>[{f.severity}]</span>{" "}
            <code>{f.file_path}:{f.line}</code>{" "}
            <span className="text-[var(--color-muted-foreground)]">· {f.agent}</span>
          </div>
          <div className="font-medium mt-0.5">{f.title}</div>
          {f.suggestion && (
            <details className="mt-1">
              <summary className="cursor-pointer text-[var(--color-muted-foreground)]">{t("reviews.suggestionToggle")}</summary>
              <pre className="mt-1 bg-[var(--color-secondary)] rounded p-2 overflow-x-auto">
                {f.suggestion}
              </pre>
            </details>
          )}
        </div>
        {/* Triage is a thumb action: full-size targets, spaced apart, and the
            dismiss reads destructive so it can't be mistaken for accept. */}
        <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setVerdict.mutate("accepted")}
            disabled={setVerdict.isPending}
            title={t("reviews.fbAcceptTitle")}
            className={
              verdict === "accepted"
                ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                : ""
            }
          >
            {t("reviews.fbAccept")}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setVerdict.mutate("dismissed")}
            disabled={setVerdict.isPending}
            title={t("reviews.fbDismissTitle")}
            className={
              verdict === "dismissed"
                ? "border-[var(--color-destructive)]/50 bg-[var(--color-destructive)]/15 text-[var(--color-destructive)]"
                : "text-[var(--color-destructive)]"
            }
          >
            {t("reviews.fbDismiss")}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={apply}
            disabled={!canApply || busy}
            title={
              canApply ? t("reviews.applyTitleCanApply")
              : f.suggestion ? t("reviews.applyTitleNonGithub")
              : t("reviews.applyTitleNoSuggestion")
            }
          >
            {busy ? "…" : t("reviews.apply")}
          </Button>
        </div>
      </div>
    </div>
  );
}

/** How many findings a run hid before posting — 0 for a run that predates
 *  the record, which gets no count rather than a false "nothing hidden". */
export function hiddenTotal(run: Pick<ReviewRunOut, "hidden"> | null | undefined): number {
  const h = run?.hidden;
  if (!h || typeof h !== "object") return 0;
  const n = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0);
  const byRule = Object.values(h.by_rule ?? {}).reduce((a, v) => a + n(v), 0);
  return byRule + n(h.duplicates) + n(h.near_duplicates) + n(h.low_confidence)
    + n(h.no_evidence) + n(h.coverage_claim) + n(h.veto);
}

/** The tooltip: what was hidden and why, rule by rule. The count alone is
 *  the claim this surface exists to stop making — "dropped 7" with no
 *  WHAT is how a filter eats true positives unnoticed. */
export function hiddenHint(
  run: Pick<ReviewRunOut, "hidden">,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  const h = run.hidden ?? {};
  const rules = Object.entries(h.by_rule ?? {})
    .filter(([, v]) => typeof v === "number" && v > 0)
    .map(([rule, v]) => `${rule} ×${v}`)
    .join(", ") || "—";
  return t("reviews.hiddenHint", {
    rules,
    duplicates: h.duplicates ?? 0,
    near: h.near_duplicates ?? 0,
    low: h.low_confidence ?? 0,
    evidence: h.no_evidence ?? 0,
    coverage: h.coverage_claim ?? 0,
    veto: h.veto ?? 0,
  });
}

function FindingsBreakdown({ run }: { run: ReviewRunOut }) {
  const t = useT();
  const total = run.findings_count;
  const hidden = hiddenTotal(run);
  return (
    <div className="flex flex-wrap items-center gap-1 text-xs">
      {run.critical > 0 && (
        <span className="text-[var(--color-destructive)] font-semibold">{t("reviews.critCount", { count: run.critical })}</span>
      )}
      {run.error > 0 && <span className="text-orange-600 font-semibold">{t("reviews.errCount", { count: run.error })}</span>}
      {run.warning > 0 && <span className="text-[var(--color-warning)]">{t("reviews.warnCount", { count: run.warning })}</span>}
      {(run.info ?? 0) > 0 && <span className="text-[var(--color-muted-foreground)]">{t("reviews.infoCount", { count: run.info ?? 0 })}</span>}
      {total === 0 && <span className="text-[var(--color-muted-foreground)]">{t("reviews.noFindingsShort")}</span>}
      {/* What the run hid before posting. Next to the counts it posted,
          because "3 findings" and "3 findings, 7 hidden" are different
          reviews, and the tooltip says which rule hid what. */}
      {hidden > 0 && (
        <span
          className="text-[var(--color-muted-foreground)] underline decoration-dotted cursor-help"
          title={hiddenHint(run, t)}
        >
          {t("reviews.hiddenCount", { count: hidden })}
        </span>
      )}
    </div>
  );
}

/** Shorthand ref the backend parses for every provider.
 *
 * `_parse_pr_ref` only recognises *URLs* on the vendor-hosted domains
 * (github.com / gitlab.com / bitbucket.org), while `provider:owner/repo#N`
 * works for self-hosted instances too — so we send the shorthand and keep
 * `pr.url` purely as a "open the PR" link. Same construction as the PR list
 * on the Repositories page. */
function prRefOf(pr: PullRequestSummary): string {
  return `${pr.provider}:${pr.repo}#${pr.number}`;
}

function ManualTrigger() {
  const token = useToken();
  const t = useT();
  const [post, setPost] = useState(true);
  // Two ways in: pick an open PR (default) or paste a ref/URL by hand for
  // repos that aren't registered here.
  const [manual, setManual] = useState(false);
  const [slug, setSlug] = useState("");
  const [prRef, setPrRef] = useState("");
  const [manualRef, setManualRef] = useState("");

  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });
  // One repo → nothing to choose, so pre-select it without an effect.
  const repoList = repos.data ?? [];
  const effSlug = slug || (repoList.length === 1 ? repoList[0].slug : "");
  // Key + URL match the Repositories page list (branch="", sort="newest"),
  // so the two views share one cache entry.
  const prs = useQuery({
    queryKey: ["pulls", effSlug, "", "newest"],
    queryFn: () =>
      api<PullRequestSummary[]>(`/api/repos/${effSlug}/pulls?sort=newest`, { token }),
    enabled: !!token && !!effSlug && !manual,
  });
  const selectedPr = (prs.data ?? []).find((p) => prRefOf(p) === prRef);
  const ref = manual ? manualRef.trim() : prRef;

  const trigger = useMutation({
    mutationFn: async (form: { pr_ref: string; post_comments: boolean }) =>
      api<ReviewRunOut>("/api/reviews/trigger", {
        method: "POST",
        token,
        json: form,
      }),
    onSuccess: (res) => {
      toast.success(t("reviews.reviewQueued", { id: res.id.slice(0, 8) }));
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("reviews.triggerTitle")}</CardTitle>
        <CardDescription>{t("reviews.mtDescription")}</CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!ref) return;
            trigger.mutate({ pr_ref: ref, post_comments: post });
          }}
        >
          {manual ? (
            <div>
              <Label htmlFor="pr-ref">{t("reviews.prRefLabel")}</Label>
              <Input
                id="pr-ref"
                name="pr_ref"
                value={manualRef}
                onChange={(e) => setManualRef(e.target.value)}
                placeholder="github:owner/repo#42  ·  https://gitlab.com/.../!17"
              />
              <p className="text-xs text-[var(--color-muted-foreground)] mt-1">
                {t("reviews.prRefHint")} <code className="font-mono">provider:owner/repo#NUM</code>.
              </p>
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              <div>
                <Label>{t("reviews.repoLabel")}</Label>
                <Select
                  className="w-full"
                  value={effSlug}
                  onChange={(v) => { setSlug(v); setPrRef(""); }}
                  placeholder={t("common.select")}
                  disabled={repoList.length === 0}
                  options={repoList.map((r) => ({ value: r.slug, label: r.full_name }))}
                />
              </div>
              {repos.isLoading ? (
                <div className="text-xs text-[var(--color-muted-foreground)]">
                  {t("common.loading")}
                </div>
              ) : repoList.length === 0 ? (
                <EmptyState
                  icon={GitPullRequestIcon}
                  title={t("reviews.mtNoRepos")}
                  description={t("reviews.mtNoReposHint")}
                />
              ) : !effSlug ? (
                <EmptyState
                  icon={GitPullRequestIcon}
                  title={t("reviews.mtPickRepo")}
                  description={t("reviews.mtPickRepoHint")}
                />
              ) : prs.isLoading ? (
                <div className="text-xs text-[var(--color-muted-foreground)]">
                  {t("common.loading")}
                </div>
              ) : prs.error ? (
                <Callout tone="danger">
                  {t("reviews.mtPrsFailed", { message: (prs.error as Error).message })}
                </Callout>
              ) : (prs.data?.length ?? 0) === 0 ? (
                <EmptyState
                  icon={GitPullRequestIcon}
                  title={t("reviews.mtNoPrs")}
                  description={t("reviews.mtNoPrsHint")}
                />
              ) : (
                <div>
                  <Label>{t("reviews.mtPrLabel")}</Label>
                  <Select
                    className="w-full"
                    value={prRef}
                    onChange={setPrRef}
                    placeholder={t("common.select")}
                    options={(prs.data ?? []).map((p) => ({
                      value: prRefOf(p),
                      label: `#${p.number} — ${p.title.length > 70 ? `${p.title.slice(0, 70)}…` : p.title}`,
                    }))}
                  />
                  {selectedPr && (
                    <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
                      <code className="font-mono">{prRef}</code>
                      <a
                        href={selectedPr.url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 underline hover:text-[var(--color-foreground)]"
                      >
                        {t("reviews.mtOpenPr")}
                        <ExternalLinkIcon className="h-3 w-3" />
                      </a>
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
          {/* Stays a text link, but gets a 44px target on a phone; -my-2 eats
              most of the added height back out of the form's rhythm. */}
          <button
            type="button"
            onClick={() => setManual((v) => !v)}
            className="self-start inline-flex min-h-11 -my-2 items-center text-xs underline text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)] sm:my-0 sm:min-h-0"
          >
            {manual ? t("reviews.mtBackToPicker") : t("reviews.mtManualLink")}
          </button>
          <div className="flex items-center justify-between rounded-md border border-[var(--color-border)] p-2.5">
            <Label htmlFor="post-toggle" className="cursor-pointer">
              {t("reviews.postComments")}
              <span className="block text-xs text-[var(--color-muted-foreground)] font-normal">
                {t("reviews.postCommentsHint")}
              </span>
            </Label>
            <Switch
              id="post-toggle"
              checked={post}
              onCheckedChange={setPost}
             
            />
          </div>
          <Button type="submit" disabled={trigger.isPending || !ref}>
            <SparklesIcon className="h-4 w-4" />
            {trigger.isPending ? t("reviews.queueingReview") : t("reviews.runReview")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
