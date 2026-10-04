"use client";

/**
 * /issues — review findings followed across a pull request's pushes.
 *
 * One row per finding per PR, keyed by a line-free fingerprint, so the same
 * defect on push 1 and push 3 is one row seen twice. A finding the next
 * commit no longer produces, in a file that commit changed, is marked fixed
 * by the pipeline itself (src/review/issues.py); the rest is closed here.
 */

import { useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { ChevronRightIcon, ExternalLinkIcon, ListChecksIcon, SearchIcon } from "lucide-react";

import {
  issuesApi,
  type IssueStatus,
  type ReviewIssue,
  type ReviewIssueList,
} from "@/lib/api";
import { clampedOffset } from "@/lib/paging";
import { useToken } from "@/lib/use-token";
import { useCanEditIssues } from "@/lib/use-analytics-access";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { WorkspaceBadge } from "@/components/workspace-badge";
import { NoReviewsYetHint } from "@/components/repo-webhook";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { QueryState } from "@/components/ui/query-state";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select } from "@/components/ui/select";
import { SeverityBadge, toSeverity } from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

const STATUSES: IssueStatus[] = ["open", "fixed", "dismissed", "resolved"];
const SEVERITIES = ["critical", "error", "warning", "info"] as const;
const CATEGORIES = [
  "bug", "security", "performance", "maintainability", "style", "other",
] as const;
const PAGE = 50;

const STATUS_VARIANT: Record<IssueStatus, "default" | "success" | "warning" | "outline"> = {
  open: "warning",
  fixed: "success",
  dismissed: "outline",
  resolved: "default",
};

/** "3d", "5h", "12m" — the age column, short on purpose. */
function age(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const m = Math.floor(ms / 60_000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

export default function IssuesPage() {
  const t = useT();
  const token = useToken();
  const canEdit = useCanEditIssues();
  const qc = useQueryClient();

  const [status, setStatus] = useState<string>("open");
  const [severity, setSeverity] = useState("");
  const [category, setCategory] = useState("");
  const [repo, setRepo] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<"newest" | "oldest" | "severity" | "last_seen">("newest");
  const [offset, setOffset] = useState(0);

  const filters = { status, severity, category, repo, q, sort, limit: PAGE, offset };
  const list = useQuery({
    queryKey: ["issues", filters],
    queryFn: () => issuesApi.list(token!, filters),
    enabled: !!token,
    placeholderData: keepPreviousData,
  });

  // A page emptied under the reader (its last row closed, or a filter
  // elsewhere shrank the list) moves back to the last page that has rows,
  // instead of showing "nothing here" with no pager to leave by.
  const shrunkTo = list.data && !list.isPlaceholderData
    ? clampedOffset(list.data.offset, list.data.total, list.data.items.length, PAGE)
    : null;
  // Adjusted during render, not in an effect: the empty page is never
  // committed. `clampedOffset` only ever moves the offset down, and the next
  // render sees placeholder data, so this settles in one step.
  if (shrunkTo !== null) setOffset(shrunkTo);

  const setIssueStatus = useMutation({
    mutationFn: ({ id, next }: { id: string; next: IssueStatus }) =>
      issuesApi.setStatus(token!, id, next),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["issues"] });
      toast.success(t("issues.statusSaved"));
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const resetPage = <T,>(set: (v: T) => void) => (v: T) => {
    set(v);
    setOffset(0);
  };

  const counts = list.data?.status_counts;
  const any = (label: string) => ({ value: "", label });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<ListChecksIcon className="h-6 w-6" />}
        title={t("issues.title")}
        badge={<WorkspaceBadge />}
        description={t("issues.subtitle")}
        tabs={<SectionTabs set="review" />}
      />

      {/* Status tabs with counts over the other filters, like a mailbox. */}
      <SegmentedControl
        semantics="tabs"
        label={t("issues.col.status")}
        value={status}
        onValueChange={(v) => resetPage(setStatus)(v)}
        segments={["", ...STATUSES].map((s) => ({
          value: s,
          label: s ? t(`issues.status.${s}`) : t("issues.all"),
          count: s ? counts?.[s as IssueStatus] : counts
            ? Object.values(counts).reduce((a, b) => a + b, 0)
            : undefined,
        }))}
        className="self-start"
      />

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        <div className="relative">
          <SearchIcon aria-hidden className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-subtle-foreground)]" />
          <Input
            aria-label={t("issues.search")}
            placeholder={t("issues.search")}
            value={q}
            onChange={(e) => resetPage(setQ)(e.target.value)}
            className="pl-9"
          />
        </div>
        <Select
          value={severity}
          onChange={resetPage(setSeverity)}
          options={[any(t("issues.anySeverity")),
            ...SEVERITIES.map((s) => ({ value: s, label: t(`issues.severity.${s}`) }))]}
        />
        <Select
          value={category}
          onChange={resetPage(setCategory)}
          options={[any(t("issues.anyCategory")),
            ...CATEGORIES.map((c) => ({ value: c, label: t(`issues.category.${c}`) }))]}
        />
        <Select
          value={repo}
          onChange={resetPage(setRepo)}
          options={[any(t("issues.anyRepo")),
            ...(list.data?.repos ?? []).map((r) => ({ value: r, label: r }))]}
        />
        <Select
          value={sort}
          onChange={(v) => resetPage(setSort)(v as typeof sort)}
          options={(["newest", "oldest", "severity", "last_seen"] as const).map((s) => ({
            value: s, label: t(`issues.sort.${s}`),
          }))}
        />
      </div>

      <Card>
        <CardContent className="px-2 pt-2 sm:px-3 sm:pt-3">
          <QueryState
            query={list}
            skeleton={6}
          >
            {(data: ReviewIssueList) =>
              data.items.length === 0 ? (
                <EmptyState
                  icon={ListChecksIcon}
                  title={t("issues.emptyTitle")}
                  description={t("issues.emptyDesc")}
                >
                  <NoReviewsYetHint />
                </EmptyState>
              ) : (
                <>
                  <Table>
                    <THead>
                      <TR>
                        <TH>{t("issues.col.status")}</TH>
                        <TH>{t("issues.col.severity")}</TH>
                        <TH>{t("issues.col.category")}</TH>
                        <TH>{t("issues.col.title")}</TH>
                        <TH>{t("issues.col.repo")}</TH>
                        <TH>{t("issues.col.file")}</TH>
                        <TH>{t("issues.col.age")}</TH>
                        <TH>{t("issues.col.pr")}</TH>
                      </TR>
                    </THead>
                    <TBody>
                      {data.items.map((i) => (
                        <IssueRow
                          key={i.id}
                          issue={i}
                          busy={setIssueStatus.isPending}
                          readOnly={canEdit === false}
                          onStatus={(next) => setIssueStatus.mutate({ id: i.id, next })}
                        />
                      ))}
                    </TBody>
                  </Table>
                  <div className="mt-2 flex items-center justify-between gap-3 border-t border-[var(--color-border)] px-2.5 py-3 text-xs tabular-nums text-[var(--color-muted-foreground)]">
                    <span>
                      {t("issues.range", {
                        from: data.total ? data.offset + 1 : 0,
                        to: data.offset + data.items.length,
                        total: data.total,
                      })}
                    </span>
                    <div className="flex gap-2">
                      <Button
                        size="sm" variant="outline"
                        disabled={offset === 0}
                        onClick={() => setOffset(Math.max(0, offset - PAGE))}
                      >
                        {t("issues.prev")}
                      </Button>
                      <Button
                        size="sm" variant="outline"
                        disabled={data.offset + data.items.length >= data.total}
                        onClick={() => setOffset(offset + PAGE)}
                      >
                        {t("issues.next")}
                      </Button>
                    </div>
                  </div>
                </>
              )
            }
          </QueryState>
        </CardContent>
      </Card>
    </PageShell>
  );
}

function IssueRow({
  issue: i, busy, readOnly, onStatus,
}: {
  issue: ReviewIssue;
  busy: boolean;
  readOnly: boolean;
  onStatus: (s: IssueStatus) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const fixedNote = i.status === "fixed" && i.resolution_source === "auto_next_commit"
    ? t("issues.fixedInCommit", { sha: (i.fixed_in_sha ?? "").slice(0, 7) })
    : null;
  return (
    <>
      <TR className={cn(open && "bg-[var(--color-accent)]/50")}>
        <TD className="whitespace-nowrap">
          <div className="flex flex-col gap-1">
            <Select
              className="h-9 w-32 text-xs sm:h-8"
              disabled={busy || readOnly}
              value={i.status}
              onChange={(v) => onStatus(v as IssueStatus)}
              options={STATUSES.map((s) => ({ value: s, label: t(`issues.status.${s}`) }))}
            />
            {fixedNote && (
              <Badge variant={STATUS_VARIANT.fixed} className="w-fit">{fixedNote}</Badge>
            )}
          </div>
        </TD>
        <TD className="whitespace-nowrap">
          <SeverityBadge severity={toSeverity(i.severity)} label={t(`issues.severity.${i.severity}`)} />
        </TD>
        <TD className="whitespace-nowrap text-[var(--color-muted-foreground)]">{t(`issues.category.${i.category}`)}</TD>
        <TD className="min-w-[16rem]">
          <button
            type="button"
            className="group/title inline-flex items-start gap-1.5 rounded text-left font-medium hover:text-[var(--color-foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            <ChevronRightIcon aria-hidden className={cn(
              "mt-0.5 h-4 w-4 shrink-0 text-[var(--color-muted-foreground)] transition-transform duration-200 ease-out-quint",
              open && "rotate-90",
            )} />
            <span className="group-hover/title:underline">{i.title}</span>
          </button>
          {i.occurrences > 1 && (
            <span className="ml-1.5 whitespace-nowrap text-xs text-[var(--color-muted-foreground)]">
              {t("issues.seenTimes", { n: i.occurrences })}
            </span>
          )}
        </TD>
        <TD className="whitespace-nowrap text-[var(--color-muted-foreground)]">{i.pr_repo || i.repo_slug}</TD>
        <TD className="max-w-[18rem] truncate font-mono text-xs text-[var(--color-muted-foreground)]" title={i.file_path}>
          {i.file_path}{i.line ? `:${i.line}` : ""}
        </TD>
        <TD className="whitespace-nowrap tabular-nums text-[var(--color-muted-foreground)]" title={formatDateTime(i.first_seen_at)}>
          {age(i.first_seen_at)}
        </TD>
        <TD className="whitespace-nowrap">
          {i.pr_url ? (
            <a
              href={i.pr_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 hover:underline"
            >
              #{i.pr_number} <ExternalLinkIcon className="h-3 w-3" />
            </a>
          ) : (
            <>#{i.pr_number}</>
          )}
          {i.pr_state && i.pr_state !== "open" && (
            <span className="ml-1.5 text-xs text-[var(--color-muted-foreground)]">
              {t(`prs.state.${i.pr_state}`)}
            </span>
          )}
        </TD>
      </TR>
      <AnimatePresence initial={false}>
        {open && (
          <TR key="detail" className="hover:bg-transparent">
            <TD colSpan={8} className="p-0">
              <m.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.26, ease: [0.23, 1, 0.32, 1] }}
                className="overflow-hidden"
              >
                <div className="space-y-2.5 bg-[var(--color-muted)]/40 px-4 py-3 text-sm">
                  {i.body && <p className="max-w-[75ch] whitespace-pre-wrap leading-relaxed">{i.body}</p>}
                  {i.suggestion && (
                    <pre className="max-w-[100ch] overflow-x-auto rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-3 font-mono text-xs leading-5">
                      {i.suggestion}
                    </pre>
                  )}
                  <p className="text-xs text-[var(--color-muted-foreground)]">
                    {t("issues.meta", {
                      agent: i.agent ?? "—",
                      first: formatDateTime(i.first_seen_at),
                      last: formatDateTime(i.last_seen_at),
                    })}
                  </p>
                </div>
              </m.div>
            </TD>
          </TR>
        )}
      </AnimatePresence>
    </>
  );
}
