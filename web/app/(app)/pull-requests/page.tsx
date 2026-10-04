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
import {
  ChevronDownIcon, ChevronRightIcon, ExternalLinkIcon, GitPullRequestIcon,
} from "lucide-react";

import {
  pullRequestsApi,
  type ReviewedPullRequest,
  type ReviewedPullRequestList,
} from "@/lib/api";
import { clampedOffset } from "@/lib/paging";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDate, formatDateTime } from "@/lib/format";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { WorkspaceBadge } from "@/components/workspace-badge";
import { NoReviewsYetHint } from "@/components/repo-webhook";
import { PullRequestReviews } from "@/components/review-timeline";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { QueryState } from "@/components/ui/query-state";
import { Select } from "@/components/ui/select";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

const PAGE = 50;

/** The last review's outcome as a badge: Success / Partial / Skipped / Failed. */
const REVIEW_VARIANT: Record<string, "success" | "warning" | "default" | "destructive"> = {
  complete: "success",
  partial: "warning",
  skipped: "default",
  failed: "destructive",
};

const STATE_VARIANT: Record<string, "brand" | "success" | "outline"> = {
  open: "brand",
  merged: "success",
  closed: "outline",
};

export default function PullRequestsPage() {
  const t = useT();
  const token = useToken();
  const [q, setQ] = useState("");
  const [repo, setRepo] = useState("");
  const [state, setState] = useState("");
  const [reviewStatus, setReviewStatus] = useState("");
  const [offset, setOffset] = useState(0);

  const filters = {
    q, repo, state, review_status: reviewStatus, limit: PAGE, offset,
  };
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

      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <Input
          aria-label={t("prs.search")}
          placeholder={t("prs.search")}
          value={q}
          onChange={(e) => pick(setQ)(e.target.value)}
        />
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
        <CardContent className="pt-4">
          <QueryState query={list} skeleton={6}>
            {(data: ReviewedPullRequestList) =>
              data.items.length === 0 ? (
                <div className="py-10 text-center">
                  <GitPullRequestIcon className="mx-auto h-8 w-8 text-[var(--color-muted-foreground)]" />
                  <div className="mt-2 text-sm font-medium">{t("prs.emptyTitle")}</div>
                  <p className="mx-auto mt-1 max-w-md text-xs text-[var(--color-muted-foreground)]">
                    {t("prs.emptyDesc")}
                  </p>
                  <NoReviewsYetHint />
                </div>
              ) : (
                <>
                  <Table>
                    <THead>
                      <TR>
                        <TH className="w-6"><span className="sr-only">{t("prs.expand")}</span></TH>
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
                  <div className="mt-3 flex items-center justify-between text-xs text-[var(--color-muted-foreground)]">
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

/** Columns of the PR table — the expanded row spans all of them. */
const COLS = 10;

function PrRow({ pr }: { pr: ReviewedPullRequest }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const sev = pr.by_severity ?? {};
  const severityTitle = (["critical", "error", "warning", "info"] as const)
    .map((s) => `${t(`issues.severity.${s}`)}: ${sev[s] ?? 0}`)
    .join(" · ");
  return (
    <Fragment>
    <TR className={open ? "bg-[var(--color-accent)]/30" : undefined}>
      <TD className="w-6">
        <button
          type="button"
          aria-expanded={open}
          aria-label={open ? t("prs.collapse") : t("prs.expand")}
          onClick={() => setOpen((v) => !v)}
          className="inline-flex h-7 w-7 items-center justify-center rounded hover:bg-[var(--color-accent)]"
        >
          {open ? <ChevronDownIcon className="h-4 w-4" /> : <ChevronRightIcon className="h-4 w-4" />}
        </button>
      </TD>
      <TD className="whitespace-nowrap font-mono">
        {pr.url ? (
          <a href={pr.url} target="_blank" rel="noreferrer"
            className="inline-flex items-center gap-1 hover:underline">
            #{pr.number} <ExternalLinkIcon className="h-3 w-3" />
          </a>
        ) : <>#{pr.number}</>}
      </TD>
      <TD className="min-w-[14rem]">
        <span className="font-medium">{pr.title || "—"}</span>{" "}
        <Badge variant={STATE_VARIANT[pr.state] ?? "outline"} className="ml-1 text-[10px]">
          {t(`prs.state.${pr.state}`)}
        </Badge>
      </TD>
      <TD className="whitespace-nowrap">{pr.repo}</TD>
      <TD className="max-w-[14rem] truncate font-mono"
        title={`${pr.base_ref ?? "?"} ← ${pr.head_ref ?? "?"}`}>
        {pr.base_ref ? <>{pr.base_ref} ← </> : null}{pr.head_ref ?? "—"}
      </TD>
      <TD className="whitespace-nowrap">{pr.author ?? "—"}</TD>
      <TD className="whitespace-nowrap" title={formatDateTime(pr.opened_at)}>
        {formatDate(pr.opened_at)}
      </TD>
      <TD className="text-right tabular-nums">{pr.reviews_count}</TD>
      <TD className="text-right tabular-nums" title={severityTitle}>
        {pr.issues_total}
        {pr.issues_open > 0 && pr.issues_open !== pr.issues_total && (
          <span className="ml-1 text-[10px] text-[var(--color-muted-foreground)]">
            ({t("prs.openCount", { n: pr.issues_open })})
          </span>
        )}
      </TD>
      <TD className="max-w-[18rem]">
        {pr.last_review_status ? (
          <div className="flex flex-col gap-0.5">
            <Badge variant={REVIEW_VARIANT[pr.last_review_status] ?? "default"}
              className="self-start">
              {t(`prs.review.${pr.last_review_status}`)}
            </Badge>
            {pr.last_review_reason && pr.last_review_status !== "complete" && (
              <span className="line-clamp-2 text-[10px] text-[var(--color-muted-foreground)]"
                title={pr.last_review_reason}>
                {pr.last_review_reason}
              </span>
            )}
          </div>
        ) : "—"}
      </TD>
    </TR>
    {open && (
      <TR className="hover:bg-transparent">
        <TD colSpan={COLS} className="bg-[var(--color-muted)]/30 px-4 py-3">
          <PullRequestReviews prId={pr.id} />
        </TD>
      </TR>
    )}
    </Fragment>
  );
}
