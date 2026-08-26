"use client";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { ArrowRightIcon, CheckIcon, GitPullRequestIcon, FolderGit2Icon, HelpCircleIcon, KeyIcon, RocketIcon, XIcon } from "lucide-react";
import { api, type ConnectionStatus, type RepoOut, type ReviewRunOut } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { startMainTour } from "@/lib/tour";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, PageHeader } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { CountUp } from "@/components/ui/count-up";
import { QueryState } from "@/components/ui/query-state";

/**
 * A metric card whose footer changes with the number above it.
 *
 * At zero a count is not a measurement, it is a question — "0 connections" and
 * a ghost "Manage" link tell a new workspace nothing about what to do, and all
 * three cards said it at once. So a zero states what is missing and offers the
 * verb; anything else keeps the quiet "Manage" it always had.
 */
function MetricCard({
  icon, label, count, loading, href, manageLabel, zeroText, zeroCta, children,
}: {
  icon: React.ReactNode;
  label: string;
  count: number;
  loading: React.ReactNode;
  href: string;
  manageLabel: string;
  zeroText: string;
  zeroCta: string;
  children?: React.ReactNode;
}) {
  const empty = count === 0;
  return (
    <Card className={empty ? "border-dashed" : undefined}>
      <CardHeader className="pb-3">
        <CardDescription className="flex items-center gap-2">{icon} {label}</CardDescription>
        {loading}
      </CardHeader>
      <CardContent>
        {empty ? (
          <>
            <p className="mb-2 text-xs text-[var(--color-muted-foreground)]">{zeroText}</p>
            <Link href={href} className="inline-flex">
              <Button size="sm">{zeroCta} <ArrowRightIcon className="ml-1 h-3 w-3" /></Button>
            </Link>
          </>
        ) : (
          <>
            {children}
            <Link href={href} className="inline-flex">
              <Button variant="ghost" size="sm" className="-ml-3">
                {manageLabel} <ArrowRightIcon className="h-3 w-3" />
              </Button>
            </Link>
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default function DashboardPage() {
  const t = useT();
  const token = useToken();
  const conns = useQuery({
    queryKey: ["connections"],
    queryFn: () => api<ConnectionStatus[]>("/api/connections", { token }),
    enabled: !!token,
  });
  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });
  const history = useQuery({
    queryKey: ["history", 10],
    queryFn: () => api<ReviewRunOut[]>("/api/reviews/history?limit=10", { token }),
    enabled: !!token,
  });

  const [dismissed, setDismissed] = useState(true);
  useEffect(() => setDismissed(localStorage.getItem("celmis:tips") === "off"), []);
  const dismissTips = () => { setDismissed(true); localStorage.setItem("celmis:tips", "off"); };

  const connectedCount = conns.data?.filter((c) => c.connected).length ?? 0;
  const repoCount = repos.data?.length ?? 0;
  const autoRepoCount =
    repos.data?.filter((r) => r.auto_review_enabled).length ?? 0;

  // The banner's steps, each ticked from live data rather than from a flag
  // somebody has to remember to set. A step already done stays visible and
  // struck through: seeing what you finished is what makes the list feel
  // short.
  const steps = [
    { done: connectedCount > 0, label: t("dash.next.s1"), href: "/connections" },
    { done: repoCount > 0, label: t("dash.next.s2"), href: "/repositories" },
    { done: (history.data?.length ?? 0) > 0, label: t("dash.next.s3"), href: "/reviews" },
  ];

  return (
    <PageShell width="wide">
      <PageHeader
        title={t("dashboard.title")}
        description={t("dashboard.subtitle")}
        actions={
          /* Tour stays reachable after the tips banner is dismissed. */
          <Button
            variant="ghost"
            size="sm"
            onClick={() => startMainTour(t)}
            title={t("tour.start")}
            aria-label={t("tour.start")}
          >
            <HelpCircleIcon className="h-4 w-4" />
          </Button>
        }
        tabs={<SectionTabs set="dashboard" />}
      />

      {!dismissed && (connectedCount === 0 || repoCount === 0) && (
        <Card className="border-[var(--color-brand)]/40 bg-[var(--color-brand-muted)]">
          <CardContent className="space-y-3 pt-6">
            <div className="flex items-start justify-between gap-4">
              <div className="flex items-center gap-2 font-medium">
                <RocketIcon className="h-4 w-4 text-[var(--color-brand)]" /> {t("dash.tips.title")}
              </div>
              <button onClick={dismissTips} aria-label="dismiss"
                className="text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]">
                <XIcon className="h-4 w-4" />
              </button>
            </div>
            {/* One line, and it is about the button at the bottom — the guided
                wizard covers workspace, LLM key and git connection, which the
                step list below deliberately does not mention. */}
            <p className="text-sm text-[var(--color-muted-foreground)]">{t("dash.tips.body")}</p>
            {/* Three clickable steps, in order. Below this used to sit a second
                paragraph reciting the sidebar — "Dashboard · Repositories ·
                Code review · Ask the code · …" — which names what is already on
                screen and answers no question anybody has on their first day.
                Where do I start, and what is left, are the two that matter. */}
            <ol className="space-y-1.5">
              {steps.map((s, i) => (
                <li key={s.href}>
                  <Link
                    href={s.href}
                    className="group flex items-center gap-2 text-sm hover:underline"
                  >
                    <span
                      className={
                        "flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[10px] font-medium " +
                        (s.done
                          ? "bg-[var(--color-brand)] text-white"
                          : "border border-[var(--color-border)] text-[var(--color-muted-foreground)]")
                      }
                    >
                      {s.done ? <CheckIcon className="h-3 w-3" /> : i + 1}
                    </span>
                    <span className={s.done ? "text-[var(--color-muted-foreground)] line-through" : undefined}>
                      {s.label}
                    </span>
                  </Link>
                </li>
              ))}
            </ol>
            <div className="flex flex-wrap items-center gap-2">
              <Link href="/onboarding">
                <Button size="sm">{t("dash.tips.cta")} <ArrowRightIcon className="ml-1 h-3.5 w-3.5" /></Button>
              </Link>
              <Button size="sm" variant="outline" onClick={() => startMainTour(t)}>
                {t("tour.start")}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-3">
        <MetricCard
          icon={<KeyIcon className="h-4 w-4" />}
          label={t("dashboard.connections")}
          count={connectedCount}
          href="/connections"
          manageLabel={t("dashboard.manage")}
          zeroText={t("dash.zero.connections")}
          zeroCta={t("dash.zero.connectionsCta")}
          loading={
            <QueryState query={conns} skeleton={1}>
              {(data) => (
                <CardTitle className="text-3xl">
                  <CountUp value={data.filter((c) => c.connected).length} />
                </CardTitle>
              )}
            </QueryState>
          }
        />

        <MetricCard
          icon={<FolderGit2Icon className="h-4 w-4" />}
          label={t("dashboard.repositories")}
          count={repoCount}
          href="/repositories"
          manageLabel={t("dashboard.manage")}
          zeroText={t("dash.zero.repos")}
          zeroCta={t("dash.zero.reposCta")}
          loading={
            <QueryState query={repos} skeleton={1}>
              {(data) => (
                <CardTitle className="text-3xl"><CountUp value={data.length} /></CardTitle>
              )}
            </QueryState>
          }
        >
          <p className="text-xs text-[var(--color-muted-foreground)] mb-2">
            {t("dashboard.autoReviewEnabled", { count: autoRepoCount })}
          </p>
        </MetricCard>

        <MetricCard
          icon={<GitPullRequestIcon className="h-4 w-4" />}
          label={t("dash.recentShown")}
          count={history.data?.length ?? 0}
          href="/reviews"
          manageLabel={t("dashboard.history")}
          zeroText={t("dash.zero.reviews")}
          zeroCta={t("dash.zero.reviewsCta")}
          loading={
            <QueryState query={history} skeleton={1}>
              {(data) => (
                <CardTitle className="text-3xl"><CountUp value={data.length} /></CardTitle>
              )}
            </QueryState>
          }
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t("dashboard.latestReviews")}</CardTitle>
          <CardDescription>{t("dashboard.latestReviewsDesc")}</CardDescription>
        </CardHeader>
        <CardContent>
          <QueryState
            query={history}
            empty={{ icon: GitPullRequestIcon, title: t("dashboard.noReviews") }}
          >
            {(runs) => (
            <div className="flex flex-col gap-2">
              {runs.map((r) => (
                <div
                  key={r.id || r.pr_ref + r.started_at}
                  className="flex items-center justify-between gap-4 rounded-md border border-[var(--color-border)] p-3 hover:bg-[var(--color-accent)]"
                >
                  <div className="flex flex-col min-w-0">
                    <code className="text-xs font-mono truncate">{r.pr_ref}</code>
                    <span className="text-xs text-[var(--color-muted-foreground)]">
                      {formatDateTime(r.started_at)}
                      {r.elapsed_seconds ? ` · ${Math.round(r.elapsed_seconds)}s` : ""}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {r.cross_repo_callers > 0 && (
                      <Badge variant="brand">{t("dashboard.crossRepoCallers", { count: r.cross_repo_callers })}</Badge>
                    )}
                    <Badge
                      variant={
                        r.verdict === "approve"
                          ? "success"
                          : r.verdict === "changes" || r.verdict === "request_changes"
                            ? "destructive"
                            : "default"
                      }
                    >
                      {r.verdict.toUpperCase()} · {r.findings_count}
                    </Badge>
                  </div>
                </div>
              ))}
            </div>
            )}
          </QueryState>
        </CardContent>
      </Card>
    </PageShell>
  );
}
