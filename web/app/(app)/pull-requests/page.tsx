"use client";

/**
 * /pull-requests — the pull requests Celmis reviewed, and what became of them.
 *
 * A row per PR (not per run): how many times it was reviewed, how the last
 * review went and why, how many issues it carries by severity, and whether it
 * was merged or closed — the state comes from the provider's webhook. A row
 * expands into its reviews, and each review into its stages.
 */

import { Fragment, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import {
  ChevronRightIcon, CircleAlertIcon, CircleCheckIcon, ClockIcon,
  ExternalLinkIcon, GitPullRequestIcon, SearchIcon,
  type LucideIcon,
} from "lucide-react";

import {
  pullRequestsApi,
  type PullRequestBucket,
  type PullRequestStats,
  type ReviewedPullRequest,
  type ReviewedPullRequestList,
} from "@/lib/api";
import { clampedOffset } from "@/lib/paging";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDate, formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { WorkspaceBadge } from "@/components/workspace-badge";
import { NoReviewsYetHint } from "@/components/repo-webhook";
import { PullRequestReviews } from "@/components/review-timeline";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { QueryState } from "@/components/ui/query-state";
import { Select } from "@/components/ui/select";
import {
  SEVERITIES, SeverityCounts, StatusPill, toRunStatus, type Severity,
} from "@/components/ui/status";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

const PAGE = 50;

/** The PR's own state, as the provider reports it. Open is a state, so it
 *  takes the info hue rather than the action colour. */
const STATE_VARIANT: Record<string, "info" | "success" | "default"> = {
  open: "info",
  merged: "success",
  closed: "default",
};

export default function PullRequestsPage() {
  const t = useT();
  const token = useToken();
  const [q, setQ] = useState("");
  const [repo, setRepo] = useState("");
  const [state, setState] = useState("");
  const [reviewStatus, setReviewStatus] = useState("");
  const [bucket, setBucket] = useState<PullRequestBucket | "">("");
  const [offset, setOffset] = useState(0);

  const filters = {
    q, repo, state, review_status: reviewStatus, bucket, limit: PAGE, offset,
  };
  // The cards follow the repository filter only: they are the overview, the
  // other filters narrow the list below them.
  const stats = useQuery({
    queryKey: ["pull-requests", "stats", repo],
    queryFn: () => pullRequestsApi.stats(token!, { repo }),
    enabled: !!token,
    placeholderData: keepPreviousData,
  });
  const list = useQuery({
    queryKey: ["pull-requests", filters],
    queryFn: () => pullRequestsApi.list(token!, filters),
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

  const pick = (set: (v: string) => void) => (v: string) => {
    set(v);
    setOffset(0);
  };

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<GitPullRequestIcon className="h-6 w-6" />}
        title={t("prs.title")}
        badge={<WorkspaceBadge />}
        description={t("prs.subtitle")}
        tabs={<SectionTabs set="review" />}
      />

      <SummaryCards
        stats={stats.data}
        active={bucket}
        onPick={(b) => { setBucket(b); setOffset(0); }}
      />

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <div className="relative">
          <SearchIcon aria-hidden className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-subtle-foreground)]" />
          <Input
            aria-label={t("prs.search")}
            placeholder={t("prs.search")}
            value={q}
            onChange={(e) => pick(setQ)(e.target.value)}
            className="pl-9"
          />
        </div>
        <Select
          value={repo}
          onChange={pick(setRepo)}
          options={[{ value: "", label: t("prs.anyRepo") },
            ...(list.data?.repos ?? []).map((r) => ({ value: r, label: r }))]}
        />
        <Select
          value={state}
          onChange={pick(setState)}
          options={[{ value: "", label: t("prs.anyState") },
            ...(["open", "merged", "closed"] as const).map((s) => ({
              value: s, label: t(`prs.state.${s}`),
            }))]}
        />
        <Select
          value={reviewStatus}
          onChange={pick(setReviewStatus)}
          options={[{ value: "", label: t("prs.anyReview") },
            ...(["complete", "partial", "skipped", "failed"] as const).map((s) => ({
              value: s, label: t(`prs.review.${s}`),
            }))]}
        />
      </div>

      <Card>
        <CardContent className="px-2 pt-2 sm:px-3 sm:pt-3">
          <QueryState query={list} skeleton={6}>
            {(data: ReviewedPullRequestList) =>
              data.items.length === 0 ? (
                <EmptyState
                  icon={GitPullRequestIcon}
                  title={t("prs.emptyTitle")}
                  description={t("prs.emptyDesc")}
                >
                  <NoReviewsYetHint />
                </EmptyState>
              ) : (
                <>
                  <Table>
                    <THead>
                      <TR>
                        <TH className="w-8"><span className="sr-only">{t("prs.expand")}</span></TH>
                        <TH>{t("prs.col.number")}</TH>
                        <TH>{t("prs.col.title")}</TH>
                        <TH>{t("prs.col.repo")}</TH>
                        <TH>{t("prs.col.branch")}</TH>
                        <TH>{t("prs.col.author")}</TH>
                        <TH>{t("prs.col.opened")}</TH>
                        <TH className="text-right">{t("prs.col.reviews")}</TH>
                        <TH className="text-right">{t("prs.col.suggestions")}</TH>
                        <TH>{t("prs.col.status")}</TH>
                      </TR>
                    </THead>
                    <TBody>
                      {data.items.map((p) => <PrRow key={p.id} pr={p} />)}
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
                      <Button size="sm" variant="outline" disabled={offset === 0}
                        onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                        {t("issues.prev")}
                      </Button>
                      <Button size="sm" variant="outline"
                        disabled={data.offset + data.items.length >= data.total}
                        onClick={() => setOffset(offset + PAGE)}>
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

const CARDS: {
  bucket: PullRequestBucket;
  icon: LucideIcon;
  /** Colour comes from the scales in globals.css: green reviewed, neutral
   *  waiting, orange needs a person. The icon says it too. */
  tone: string;
}[] = [
  { bucket: "reviewed_today", icon: CircleCheckIcon,
    tone: "bg-[var(--color-success-soft)] text-[var(--color-success)]" },
  { bucket: "awaiting", icon: ClockIcon,
    tone: "bg-[var(--color-neutral-soft)] text-[var(--color-muted-foreground)]" },
  { bucket: "attention", icon: CircleAlertIcon,
    tone: "bg-[var(--color-attention-soft)] text-[var(--color-attention)]" },
];

const CARD_KEY = {
  reviewed_today: "reviewedToday",
  awaiting: "awaiting",
  attention: "attention",
} as const;

/** Three counters above the filters. A card is a toggle: clicking it narrows
 *  the list to exactly the PRs it counted, clicking it again clears that. */
function SummaryCards({ stats, active, onPick }: {
  stats: PullRequestStats | undefined;
  active: PullRequestBucket | "";
  onPick: (b: PullRequestBucket | "") => void;
}) {
  const t = useT();
  return (
    <div className="grid gap-2 sm:grid-cols-3" role="group" aria-label={t("prs.title")}>
      {CARDS.map(({ bucket, icon: Icon, tone }) => {
        const key = CARD_KEY[bucket];
        const on = active === bucket;
        const hint = t(`prs.card.${key}Hint`);
        return (
          <button
            key={bucket}
            type="button"
            aria-pressed={on}
            title={on ? `${hint} ${t("prs.card.filterOn")}` : hint}
            onClick={() => onPick(on ? "" : bucket)}
            className={cn(
              "flex items-center gap-3 rounded-xl border bg-[var(--color-card)] px-4 py-3 text-left transition-colors",
              "hover:bg-[var(--color-accent)]/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]",
              on ? "border-[var(--color-ring)]" : "border-[var(--color-border)]",
            )}
          >
            <span className={cn("inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg", tone)}>
              <Icon aria-hidden className="h-5 w-5" />
            </span>
            <span className="flex min-w-0 flex-col">
              <span className="truncate text-xs text-[var(--color-muted-foreground)]">
                {t(`prs.card.${key}`)}
              </span>
              <span className="text-2xl font-semibold leading-tight tabular-nums">
                {stats ? stats[bucket] : "–"}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** Columns of the PR table — the expanded row spans all of them. */
const COLS = 10;

function PrRow({ pr }: { pr: ReviewedPullRequest }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const sev = pr.by_severity ?? {};
  const severityTitle = SEVERITIES
    .map((s) => `${t(`issues.severity.${s}`)}: ${sev[s] ?? 0}`)
    .join(" · ");
  const severityLabels = Object.fromEntries(
    SEVERITIES.map((s) => [s, t(`issues.severity.${s}`)]),
  ) as Record<Severity, string>;
  return (
    <Fragment>
    <TR className={cn(open && "bg-[var(--color-accent)]/50")}>
      <TD className="w-8 align-middle">
        <button
          type="button"
          aria-expanded={open}
          aria-label={open ? t("prs.collapse") : t("prs.expand")}
          onClick={() => setOpen((v) => !v)}
          className="inline-flex h-8 w-8 items-center justify-center rounded-md text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-selected)] hover:text-[var(--color-foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]"
        >
          <ChevronRightIcon className={cn(
            "h-4 w-4 transition-transform duration-200 ease-out-quint",
            open && "rotate-90",
          )} />
        </button>
      </TD>
      <TD className="whitespace-nowrap align-middle font-mono text-xs">
        {pr.url ? (
          <a href={pr.url} target="_blank" rel="noreferrer"
            className="inline-flex items-center gap-1 text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)] hover:underline">
            #{pr.number} <ExternalLinkIcon className="h-3 w-3" />
          </a>
        ) : <span className="text-[var(--color-muted-foreground)]">#{pr.number}</span>}
      </TD>
      <TD className="min-w-[14rem] align-middle">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-medium">{pr.title || "—"}</span>
          <Badge variant={STATE_VARIANT[pr.state] ?? "default"}>
            {t(`prs.state.${pr.state}`)}
          </Badge>
        </span>
      </TD>
      <TD className="whitespace-nowrap align-middle text-[var(--color-muted-foreground)]">{pr.repo}</TD>
      <TD className="max-w-[14rem] truncate align-middle font-mono text-xs text-[var(--color-muted-foreground)]"
        title={`${pr.base_ref ?? "?"} ← ${pr.head_ref ?? "?"}`}>
        {pr.base_ref ? <>{pr.base_ref} ← </> : null}{pr.head_ref ?? "—"}
      </TD>
      <TD className="whitespace-nowrap align-middle">{pr.author ?? "—"}</TD>
      <TD className="whitespace-nowrap align-middle text-[var(--color-muted-foreground)]" title={formatDateTime(pr.opened_at)}>
        {formatDate(pr.opened_at)}
      </TD>
      <TD className="text-right align-middle tabular-nums">{pr.reviews_count}</TD>
      <TD className="align-middle" title={severityTitle}>
        {/* The severity mix is on the row, not only in a tooltip: a
            critical finding is the first thing a lead scans for. */}
        <span className="flex flex-wrap items-center justify-end gap-1.5">
          <SeverityCounts counts={sev} labels={severityLabels} />
          <span className="font-medium tabular-nums">{pr.issues_total}</span>
          {pr.issues_open > 0 && pr.issues_open !== pr.issues_total && (
            <span className="text-xs text-[var(--color-muted-foreground)]">
              ({t("prs.openCount", { n: pr.issues_open })})
            </span>
          )}
        </span>
      </TD>
      <TD className="max-w-[18rem] align-middle">
        {pr.last_review_status ? (
          <div className="flex flex-col gap-1">
            <StatusPill
              status={toRunStatus(pr.last_review_status)}
              label={t(`prs.review.${pr.last_review_status}`)}
              className="self-start"
            />
            {pr.last_review_reason && pr.last_review_status !== "complete" && (
              <span className="line-clamp-2 text-xs leading-snug text-[var(--color-muted-foreground)]"
                title={pr.last_review_reason}>
                {pr.last_review_reason}
              </span>
            )}
          </div>
        ) : "—"}
      </TD>
    </TR>
    <AnimatePresence initial={false}>
      {open && (
        <TR key="reviews" className="hover:bg-transparent">
          <TD colSpan={COLS} className="p-0">
            <m.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.28, ease: [0.23, 1, 0.32, 1] }}
              className="overflow-hidden"
            >
              <div className="bg-[var(--color-muted)]/40 px-3 py-3 sm:pl-12 sm:pr-4">
                <PullRequestReviews prId={pr.id} />
              </div>
            </m.div>
          </TD>
        </TR>
      )}
    </AnimatePresence>
    </Fragment>
  );
}
