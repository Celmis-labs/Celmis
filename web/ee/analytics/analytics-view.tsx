"use client";

// Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
// see LICENSING.md and ee/README.md in the repository root.

/**
 * /analytics — what the reviewer did for this workspace, and what came of it.
 *
 * Enterprise: rendered by the AGPL route `app/(app)/analytics/page.tsx` only
 * when /api/capabilities does not report `review_analytics` off — the API
 * mounts /api/analytics only under a licence that grants "analytics".
 *
 * Owner, admin or editor of the workspace (or a global admin): it reports
 * cost and how often the team merged what the reviewer flagged. The API
 * refuses everyone else (`require_analytics_access`); the hook below only
 * decides what to draw.
 *
 * Charts are plain SVG/CSS — one series each, so no legend: the card title
 * names it. Every bar carries a <title> for hover, and each chart has a table
 * view underneath for anyone who cannot or would rather not read a chart.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BarChart3Icon, ShieldOffIcon } from "lucide-react";

import { analyticsApi, type AnalyticsSummary } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDate } from "@/lib/format";
import { useCanViewAnalytics } from "@/lib/use-analytics-access";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { WorkspaceBadge } from "@/components/workspace-badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { QueryState } from "@/components/ui/query-state";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Skeleton } from "@/components/ui/skeleton";
import { SEVERITY_COLOR, SeverityIcon } from "@/components/ui/status";

const WINDOWS = [7, 30, 90] as const;
type Window = (typeof WINDOWS)[number];

function money(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v < 0.01 && v > 0 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

function seconds(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (v < 90) return `${Math.round(v)}s`;
  return `${(v / 60).toFixed(1)}m`;
}

export function AnalyticsView() {
  const t = useT();
  const token = useToken();
  const allowed = useCanViewAnalytics();
  const [days, setDays] = useState<Window>(30);

  const summary = useQuery({
    queryKey: ["analytics", days],
    queryFn: () => analyticsApi.summary(token!, days),
    enabled: !!token && allowed === true,
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<BarChart3Icon className="h-6 w-6" />}
        title={t("analytics.title")}
        badge={<WorkspaceBadge />}
        description={t("analytics.subtitle")}
        tabs={<SectionTabs set="review" />}
        actions={allowed ? (
          <SegmentedControl
            label={t("analytics.window")}
            size="sm"
            value={String(days)}
            onValueChange={(v) => setDays(Number(v) as Window)}
            segments={WINDOWS.map((d) => ({ value: String(d), label: t("analytics.days", { n: d }) }))}
          />
        ) : undefined}
      />

      {allowed === undefined && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-28 rounded-xl" />)}
        </div>
      )}

      {allowed === false && (
        <Card>
          <CardContent>
            <EmptyState
              icon={ShieldOffIcon}
              title={t("analytics.forbiddenTitle")}
              description={t("analytics.forbiddenDesc")}
            />
          </CardContent>
        </Card>
      )}

      {allowed === true && (
        <QueryState query={summary} skeleton={4}>
          {(s) => <Dashboard s={s} />}
        </QueryState>
      )}
    </PageShell>
  );
}

function Kpi({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card className="flex flex-col">
      <CardContent className="flex flex-1 flex-col pt-5 sm:pt-5">
        <div className="text-sm font-medium text-[var(--color-muted-foreground)]">{label}</div>
        <div className="mt-2 text-3xl font-semibold tracking-tight tabular-nums">{value}</div>
        {hint && (
          <div className="mt-auto pt-2 text-xs leading-relaxed text-[var(--color-muted-foreground)]">{hint}</div>
        )}
      </CardContent>
    </Card>
  );
}

function Dashboard({ s }: { s: AnalyticsSummary }) {
  const t = useT();
  const oc = s.outcomes;
  const sev = s.findings_by_severity;
  const totalFindings = sev.critical + sev.error + sev.warning + sev.info;

  if (s.reviews.total === 0 && oc.found === 0) {
    return (
      <Card>
        <CardContent>
          <EmptyState
            icon={BarChart3Icon}
            title={t("analytics.emptyTitle")}
            description={t("analytics.emptyDesc", { n: s.days })}
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi
          label={t("analytics.kpi.reviews")}
          value={String(s.reviews.total)}
          hint={t("analytics.kpi.reviewsHint", {
            failed: s.reviews.by_status.failed ?? 0,
            skipped: s.reviews.by_status.skipped ?? 0,
          })}
        />
        <Kpi
          label={t("analytics.kpi.time")}
          value={seconds(s.review_time_seconds.avg)}
          hint={t("analytics.kpi.timeHint", {
            p50: seconds(s.review_time_seconds.p50),
            p90: seconds(s.review_time_seconds.p90),
          })}
        />
        {/* What reviews cost is the payer's: the API leaves `cost_usd` out for
            anyone below owner/admin, and an absent figure is hidden, never
            drawn as $0. */}
        {s.cost_usd && (
          <Kpi
            label={t("analytics.kpi.cost")}
            // No run with a known price: "$0.00" would read as free.
            value={s.cost_usd.runs_with_cost ? money(s.cost_usd.total) : "—"}
            hint={t("analytics.kpi.costHint", {
              avg: money(s.cost_usd.avg_per_review),
              unknown: s.cost_usd.runs_with_unknown_cost,
            })}
          />
        )}
        <Kpi
          label={t("analytics.kpi.fixRate")}
          value={oc.fix_rate_pct === null ? "—" : `${oc.fix_rate_pct}%`}
          hint={t("analytics.kpi.fixRateHint", {
            fixed: oc.fixed_any, found: oc.found - oc.dismissed,
          })}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t("analytics.outcomesTitle")}</CardTitle>
          <CardDescription>{t("analytics.outcomesDesc")}</CardDescription>
        </CardHeader>
        <CardContent>
          <OutcomeBar s={s} />
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>{t("analytics.reviewsPerDay")}</CardTitle>
          </CardHeader>
          <CardContent>
            <DailyBars s={s} field="reviews" />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>{t("analytics.findingsPerDay")}</CardTitle>
          </CardHeader>
          <CardContent>
            <DailyBars s={s} field="findings" />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>{t("analytics.bySeverity")}</CardTitle>
            <CardDescription>{t("analytics.bySeverityDesc", { n: totalFindings })}</CardDescription>
          </CardHeader>
          <CardContent>
            {/* Each severity in its own colour, with its icon by the label:
                the one chart here where hue carries meaning. */}
            <HBars
              rows={(["critical", "error", "warning", "info"] as const).map((k) => ({
                label: t(`issues.severity.${k}`), value: sev[k],
                color: SEVERITY_COLOR[k],
                icon: <SeverityIcon severity={k} className="size-3.5" />,
              }))}
            />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>{t("analytics.byCategory")}</CardTitle>
            <CardDescription>{t("analytics.byCategoryDesc")}</CardDescription>
          </CardHeader>
          <CardContent>
            <HBars
              rows={Object.entries(s.issues_by_category)
                .map(([k, v]) => ({ label: t(`issues.category.${k}`), value: v }))
                .sort((a, b) => b.value - a.value)}
            />
          </CardContent>
        </Card>
      </div>

      {s.cost_usd && (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("analytics.costBasis")}
        </p>
      )}
    </div>
  );
}

/** Found → fixed in a later commit / open on merged PRs / still open /
 *  dismissed, as one stacked bar with a 2px surface gap between segments.
 *  Every segment is also a labelled row beneath, so nothing is color-alone. */
function OutcomeBar({ s }: { s: AnalyticsSummary }) {
  const t = useT();
  const oc = s.outcomes;
  const openElsewhere = Math.max(0, oc.still_open - oc.open_on_merged_prs);
  const fixedOther = Math.max(0, oc.fixed_any - oc.fixed_in_next_commits);
  const segments = [
    { key: "fixedNext", value: oc.fixed_in_next_commits, color: "var(--color-success)" },
    { key: "fixedOther", value: fixedOther, color: "color-mix(in oklab, var(--color-success) 50%, var(--color-card))" },
    { key: "openMerged", value: oc.open_on_merged_prs, color: "var(--color-destructive)" },
    { key: "openOther", value: openElsewhere, color: "var(--color-warning)" },
    { key: "dismissed", value: oc.dismissed, color: "var(--color-input)" },
  ];
  const total = segments.reduce((a, b) => a + b.value, 0);
  if (total === 0) {
    return <p className="text-sm text-[var(--color-muted-foreground)]">{t("analytics.noIssues")}</p>;
  }
  return (
    <div className="space-y-3">
      <div
        className="flex h-3 w-full gap-[2px] overflow-hidden rounded-full"
        role="img"
        aria-label={segments.map((g) => `${t(`analytics.outcome.${g.key}`)}: ${g.value}`).join(", ")}
      >
        {segments.filter((g) => g.value > 0).map((g) => (
          <div
            key={g.key}
            className="transition-[width] duration-500 ease-out-quint first:rounded-l-full last:rounded-r-full"
            style={{ width: `${(100 * g.value) / total}%`, background: g.color }}
            title={`${t(`analytics.outcome.${g.key}`)}: ${g.value}`}
          />
        ))}
      </div>
      <ul className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-5">
        {segments.map((g) => (
          <li key={g.key} className="flex items-center gap-2">
            <span aria-hidden className="inline-block size-2.5 shrink-0 rounded-full" style={{ background: g.color }} />
            <span className="text-[var(--color-muted-foreground)]">{t(`analytics.outcome.${g.key}`)}</span>
            <span className="ml-auto font-semibold tabular-nums sm:ml-0">{g.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** One series per day, a column per day of the window, zero days included. */
function DailyBars({ s, field }: { s: AnalyticsSummary; field: "reviews" | "findings" }) {
  const t = useT();
  const data = s.daily;
  const max = Math.max(1, ...data.map((d) => d[field]));
  const W = 600;
  const H = 140;
  const gap = data.length > 40 ? 1 : 2;
  const bw = Math.max(1, W / data.length - gap);
  return (
    <div>
      <svg
        viewBox={`0 0 ${W} ${H + 16}`}
        className="h-40 w-full"
        role="img"
        aria-label={t(field === "reviews" ? "analytics.reviewsPerDay" : "analytics.findingsPerDay")}
        preserveAspectRatio="none"
      >
        {/* Quarter gridlines, faint: enough to read a height against. */}
        {[0.25, 0.5, 0.75].map((r) => (
          <line key={r} x1={0} x2={W} y1={H - r * (H - 8)} y2={H - r * (H - 8)}
            stroke="var(--color-border)" strokeWidth={1} strokeDasharray="3 4" />
        ))}
        <line x1={0} x2={W} y1={H} y2={H} stroke="var(--color-border-strong)" strokeWidth={1} />
        {data.map((d, i) => {
          const h = (d[field] / max) * (H - 8);
          return (
            <g key={d.date}>
              {/* Hit target the full column height, bigger than the mark. */}
              <rect x={i * (bw + gap)} y={0} width={bw + gap} height={H} fill="transparent">
                <title>{`${formatDate(d.date)}: ${d[field]}`}</title>
              </rect>
              {d[field] > 0 && (
                <rect
                  x={i * (bw + gap)}
                  y={H - h}
                  width={bw}
                  height={h}
                  rx={Math.min(3, bw / 2)}
                  fill="var(--color-primary)"
                  fillOpacity={0.85}
                  pointerEvents="none"
                />
              )}
            </g>
          );
        })}
        <text x={0} y={H + 13} fontSize={11} fill="var(--color-muted-foreground)">
          {formatDate(data[0]?.date)}
        </text>
        <text x={W} y={H + 13} fontSize={11} textAnchor="end" fill="var(--color-muted-foreground)">
          {formatDate(data[data.length - 1]?.date)}
        </text>
      </svg>
      <div className="mt-1 text-xs tabular-nums text-[var(--color-muted-foreground)]">
        {t("analytics.peak", { n: max === 1 && !data.some((d) => d[field]) ? 0 : max })}
      </div>
      <details className="mt-2 text-xs">
        <summary className="w-fit cursor-pointer rounded text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]">
          {t("analytics.tableView")}
        </summary>
        <table className="mt-2 w-full">
          <tbody>
            {data.filter((d) => d[field] > 0).map((d) => (
              <tr key={d.date}>
                <td className="py-0.5">{formatDate(d.date)}</td>
                <td className="py-0.5 text-right tabular-nums">{d[field]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

/** Labelled horizontal bars — the label and the number are text, the bar is
 *  only the magnitude. */
function HBars({ rows }: {
  rows: Array<{ label: string; value: number; color?: string; icon?: React.ReactNode }>;
}) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <ul className="space-y-2.5">
      {rows.map((r) => (
        <li key={r.label} className="grid grid-cols-[8rem_1fr_3rem] items-center gap-3 text-sm">
          <span className="flex min-w-0 items-center gap-1.5 text-[var(--color-muted-foreground)]"
            style={r.color ? { color: r.color } : undefined}>
            {r.icon}
            <span className="truncate text-[var(--color-muted-foreground)]">{r.label}</span>
          </span>
          <span className="h-2 rounded-full bg-[var(--color-muted)]" title={`${r.label}: ${r.value}`}>
            <span
              className="block h-full rounded-full transition-[width] duration-500 ease-out-quint"
              style={{ width: `${(100 * r.value) / max}%`, background: r.color ?? "var(--color-primary)" }}
            />
          </span>
          <span className="text-right font-semibold tabular-nums">{r.value}</span>
        </li>
      ))}
    </ul>
  );
}
