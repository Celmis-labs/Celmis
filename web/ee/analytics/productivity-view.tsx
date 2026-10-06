"use client";

// Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
// see LICENSING.md and ee/README.md in the repository root.

/**
 * /productivity — how fast pull requests move from first commit to
 * production, and how stable the releases are: cycle time split into coding,
 * pickup and review, the four DORA figures, PR size, throughput, and who does
 * the work.
 *
 * Enterprise: rendered by the AGPL route `app/(app)/productivity/page.tsx`
 * only when /api/capabilities does not report `productivity` off — the API
 * mounts these endpoints under the same licence feature as /analytics.
 *
 * Owner or admin only (it names people and holds the sync settings). The API
 * refuses everyone else; the hook below only decides what to draw.
 *
 * Every figure comes with the same figure for the previous equal period, and
 * is a median (with p75/p90 in the hint): a mean of cycle times is a number
 * about the slowest PR. Medians of fewer than three PRs are not shown per
 * person. Where the data is partial (history still loading, PRs not read in
 * detail yet) the page says so rather than showing a smaller truth.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDownRightIcon, ArrowUpRightIcon, ExternalLinkIcon, GaugeIcon, ShieldOffIcon } from "lucide-react";

import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { WorkspaceBadge } from "@/components/workspace-badge";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { InlineHelp } from "@/components/ui/inline-help";
import { QueryState } from "@/components/ui/query-state";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import {
  DeployTimeline, LineSeries, SizeBars, StackedBars, duration, type Series,
} from "@/ee/analytics/productivity-charts";
import {
  PRODUCTIVITY_WINDOWS, productivityApi, type Band, type Figure, type ProductivityFilters,
  type ProductivityOverview, type ProductivityWindow,
} from "@/ee/analytics/productivity-api";
import { SyncPanel, useSyncOverview } from "@/ee/analytics/productivity-sync";
import { formatDate } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useCanManageWorkspace } from "@/lib/use-workspace-role";
import { useToken } from "@/lib/use-token";

const SORTS = ["cycle", "size", "pickup", "review"] as const;
type Sort = (typeof SORTS)[number];

const BAND_VARIANT: Record<Band, BadgeVariant> = {
  elite: "success", high: "success", medium: "warning", low: "destructive",
};

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const lines = (v: number | null | undefined) => (v === null || v === undefined ? "—" : String(Math.round(v)));
const perWeek = (v: number | null | undefined) => (v === null || v === undefined ? "—" : (v < 10 ? v.toFixed(1) : String(Math.round(v))));

export function ProductivityView() {
  const t = useT();
  const token = useToken();
  // Owner or admin only, as the API enforces: the page is not a lead's view,
  // it lists people and holds the sync settings.
  const allowed = useCanManageWorkspace();
  const [days, setDays] = useState<ProductivityWindow>(30);
  const [filters, setFilters] = useState<ProductivityFilters>({});
  const [sort, setSort] = useState<Sort>("cycle");
  const on = !!token && allowed === true;

  const options = useQuery({
    queryKey: ["productivity", "filters"],
    queryFn: () => productivityApi.filters(token!),
    enabled: on,
  });
  const sync = useSyncOverview(on);
  const overview = useQuery({
    queryKey: ["productivity", "overview", days, filters],
    queryFn: () => productivityApi.overview(token!, days, filters),
    enabled: on,
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<GaugeIcon className="h-6 w-6" />}
        title={t("productivity.title")}
        badge={<WorkspaceBadge />}
        description={t("productivity.subtitle")}
        tabs={<SectionTabs set="review" />}
        actions={allowed ? (
          <SegmentedControl
            label={t("analytics.window")}
            size="sm"
            value={String(days)}
            onValueChange={(v) => setDays(Number(v) as ProductivityWindow)}
            segments={PRODUCTIVITY_WINDOWS.map((d) => ({ value: String(d), label: t("analytics.days", { n: d }) }))}
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
            <EmptyState icon={ShieldOffIcon} title={t("productivity.forbiddenTitle")} description={t("productivity.forbiddenDesc")} />
          </CardContent>
        </Card>
      )}

      {allowed === true && (
        <div className="space-y-6">
          <DataNotes sync={sync.data} />
          {sync.data && !sync.data.any_enabled && (
            <Card>
              <CardContent>
                <EmptyState icon={GaugeIcon} title={t("productivity.setupTitle")} description={t("productivity.setupDesc")} />
              </CardContent>
            </Card>
          )}
          <FilterBar value={filters} onChange={setFilters} options={options.data} />
          <QueryState query={overview} skeleton={4}>
            {(o) => <Dashboard o={o} />}
          </QueryState>
          <PrTable days={days} filters={filters} sort={sort} onSort={setSort} />
          <DeveloperTable days={days} filters={filters} />
          <SyncPanel sync={sync.data} />
        </div>
      )}
    </PageShell>
  );
}

// ─── notices ─────────────────────────────────────────────────────────

function DataNotes({ sync }: { sync: ReturnType<typeof useSyncOverview>["data"] }) {
  const t = useT();
  if (!sync) return null;
  const done = sync.repos.reduce((a, r) => a + (r.enabled ? r.status?.prs_detailed ?? 0 : 0), 0);
  const total = sync.repos.reduce((a, r) => a + (r.enabled ? r.status?.prs_total ?? 0 : 0), 0);
  const limited = sync.rate_limited_until && new Date(sync.rate_limited_until) > new Date();
  return (
    <>
      {sync.backfilling && sync.any_enabled && (
        <Callout tone="info">{t("productivity.sync.backfilling", { done, total })}</Callout>
      )}
      {limited && <Callout tone="warning">{t("productivity.sync.limitedUntil", { when: formatDate(sync.rate_limited_until) })}</Callout>}
    </>
  );
}

// ─── filters ─────────────────────────────────────────────────────────

function FilterBar({ value, onChange, options }: {
  value: ProductivityFilters;
  onChange: (f: ProductivityFilters) => void;
  options: Awaited<ReturnType<typeof productivityApi.filters>> | undefined;
}) {
  const t = useT();
  const set = (k: keyof ProductivityFilters) => (v: string) => onChange({ ...value, [k]: v || undefined });
  const all = (label: string, rest: Array<{ value: string; label: string }>) => [{ value: "", label }, ...rest];
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      <Select value={value.repo ?? ""} onChange={set("repo")} className="w-full"
        options={all(t("productivity.filter.allRepos"), (options?.repos ?? []).map((r) => ({ value: r.repo, label: r.repo })))} />
      <Select value={value.group ?? ""} onChange={set("group")} className="w-full"
        options={all(t("productivity.filter.allGroups"), (options?.groups ?? []).map((g) => ({ value: g, label: g })))} />
      <Select value={value.author ?? ""} onChange={set("author")} className="w-full"
        options={all(t("productivity.filter.allAuthors"), (options?.authors ?? []).map((a) => ({ value: a.key, label: a.name })))} />
      <Select value={value.target ?? ""} onChange={set("target")} className="w-full"
        options={all(t("productivity.filter.allTargets"), (options?.targets ?? []).map((b) => ({ value: b, label: b })))} />
    </div>
  );
}

// ─── the dashboard ───────────────────────────────────────────────────

type Tone = "up" | "down" | "none";

/** The change against the previous period. `good` says which direction is the better one. */
function Delta({ f, good }: { f: Figure; good: Tone }) {
  const t = useT();
  if (f.delta === null || f.delta === undefined) return null;
  const rounded = Math.round(f.delta * 100);
  if (rounded === 0) return <span className="text-xs text-[var(--color-muted-foreground)]">±0%</span>;
  const up = rounded > 0;
  const better = good === "none" ? null : (good === "up") === up;
  const color = better === null ? "text-[var(--color-muted-foreground)]"
    : better ? "text-[var(--color-success)]" : "text-[var(--color-destructive)]";
  const Icon = up ? ArrowUpRightIcon : ArrowDownRightIcon;
  return (
    <span className={`inline-flex items-center gap-0.5 text-xs font-medium tabular-nums ${color}`}>
      <Icon className="size-3.5" aria-hidden />
      {`${up ? "+" : "−"}${Math.abs(rounded)}%`}
      <span className="sr-only">{t("productivity.vsPrevious")}</span>
    </span>
  );
}

function Kpi({ label, value, hint, delta, good, band, definition }: {
  label: string; value: string; hint?: string; delta?: Figure; good: Tone; band?: Band | null; definition: string;
}) {
  const t = useT();
  return (
    <Card className="flex flex-col">
      <CardContent className="flex flex-1 flex-col pt-5 sm:pt-5">
        <div className="flex items-start justify-between gap-2">
          <div className="text-sm font-medium text-[var(--color-muted-foreground)]" title={definition}>{label}</div>
          {band && <Badge variant={BAND_VARIANT[band]}>{t("productivity.band", { band: t(`productivity.band.${band}`) })}</Badge>}
        </div>
        <div className="mt-2 flex items-baseline gap-2">
          <div className="text-3xl font-semibold tracking-tight tabular-nums">{value}</div>
          {delta && <Delta f={delta} good={good} />}
        </div>
        {hint && <div className="mt-auto pt-2 text-xs leading-relaxed text-[var(--color-muted-foreground)]">{hint}</div>}
      </CardContent>
    </Card>
  );
}

function ChartCard({ title, desc, children }: { title: string; desc?: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {desc && <CardDescription>{desc}</CardDescription>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function Dashboard({ o }: { o: ProductivityOverview }) {
  const t = useT();
  const k = o.kpis;
  const impl = k.implementation_rate;
  const empty = k.merged_prs.value === 0 && k.deploy_frequency.total === 0 && k.opened_prs.value === 0;

  if (empty) {
    return (
      <Card>
        <CardContent>
          <EmptyState icon={GaugeIcon} title={t("productivity.emptyTitle")} description={t("productivity.emptyDesc", { n: o.days })} />
        </CardContent>
      </Card>
    );
  }

  const percentiles = (f: Figure) => t("productivity.hint.percentiles", { p75: duration(f.p75), p90: duration(f.p90), n: f.n ?? 0 });
  const cycleSeries: Series[] = [
    { key: "coding", label: t("productivity.coding"), color: "var(--color-info)" },
    { key: "pickup", label: t("productivity.pickup"), color: "var(--color-warning)" },
    { key: "review", label: t("productivity.review"), color: "var(--color-primary)" },
  ];
  const flowSeries: Series[] = [
    { key: "opened", label: t("productivity.opened"), color: "var(--color-info)" },
    { key: "merged", label: t("productivity.merged"), color: "var(--color-primary)" },
    { key: "declined", label: t("productivity.declined"), color: "var(--color-muted-foreground)" },
  ];
  const deploySeries: Series[] = [
    { key: "ok", label: t("productivity.deployOk"), color: "var(--color-primary)" },
    { key: "failed", label: t("productivity.deployFailed"), color: "var(--color-destructive)" },
  ];
  const b = o.breakdown;

  return (
    <div className="space-y-6">
      {(o.truncated || o.previous_incomplete || b.not_enriched > 0 || b.merged_without_review > 0) && (
        <Callout tone="info">
          <ul className="space-y-0.5">
            {o.truncated && <li>{t("productivity.note.truncated")}</li>}
            {o.previous_incomplete && o.covered_from && (
              <li>{t("productivity.note.previousIncomplete", { date: o.covered_from.slice(0, 10) })}</li>
            )}
            {b.not_enriched > 0 && <li>{t("productivity.note.notEnriched", { n: b.not_enriched })}</li>}
            {b.merged_without_review > 0 && <li>{t("productivity.note.noReview", { n: b.merged_without_review })}</li>}
          </ul>
        </Callout>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi label={t("productivity.kpi.cycle")} value={duration(k.cycle_time.value)} delta={k.cycle_time} good="down"
          hint={percentiles(k.cycle_time)} definition={t("productivity.def.cycle")} />
        <Kpi label={t("productivity.kpi.lead")} value={duration(k.lead_time.value)} delta={k.lead_time} good="down"
          band={k.lead_time.band}
          hint={k.lead_time.undeployed_merged ? t("productivity.hint.undeployed", { n: k.lead_time.undeployed_merged }) : percentiles(k.lead_time)}
          definition={t("productivity.def.lead")} />
        <Kpi label={t("productivity.kpi.deploys")} value={perWeek(k.deploy_frequency.value)} delta={k.deploy_frequency} good="up"
          band={k.deploy_frequency.band}
          hint={t("productivity.hint.deploys", { n: k.deploy_frequency.total, days: o.days })}
          definition={t("productivity.def.deploys")} />
        <Kpi label={t("productivity.kpi.cfr")} value={pct(k.change_failure_rate.value)} delta={k.change_failure_rate} good="down"
          band={k.change_failure_rate.band}
          hint={t("productivity.hint.cfr", {
            failed: k.change_failure_rate.failed, settled: k.change_failure_rate.settled, recent: k.change_failure_rate.unsettled,
          })}
          definition={t("productivity.def.cfr")} />
        <Kpi label={t("productivity.kpi.recover")} value={duration(k.time_to_recover.value)} delta={k.time_to_recover} good="down"
          band={k.time_to_recover.band} hint={percentiles(k.time_to_recover)} definition={t("productivity.def.recover")} />
        <Kpi label={t("productivity.kpi.merged")} value={String(k.merged_prs.value ?? 0)} delta={k.merged_prs} good="up"
          hint={t("productivity.hint.opened", { n: k.opened_prs.value ?? 0 })} definition={t("productivity.def.merged")} />
        <Kpi label={t("productivity.kpi.size")} value={lines(k.pr_size.value)} delta={k.pr_size} good="down"
          hint={t("productivity.hint.size", { p90: lines(k.pr_size.p90) })} definition={t("productivity.def.size")} />
        <Kpi label={t("productivity.kpi.implementation")}
          value={impl.available ? pct(impl.value) : "—"} delta={impl.available ? impl : undefined} good="up"
          hint={impl.available
            ? t("productivity.hint.implementation", { taken: impl.implemented, notTaken: impl.unimplemented })
            : t("productivity.hint.implementationOff")}
          definition={t("productivity.def.implementation")} />
      </div>

      <InlineHelp question={t("productivity.def.question")}>
        <dl className="mt-1 grid gap-x-6 gap-y-2 sm:grid-cols-2">
          {(["cycle", "lead", "deploys", "cfr", "recover", "merged", "size", "implementation"] as const).map((key) => (
            <div key={key}>
              <dt className="font-medium">{t(`productivity.kpi.${key}`)}</dt>
              <dd className="text-[var(--color-muted-foreground)]">{t(`productivity.def.${key}`)}</dd>
            </div>
          ))}
        </dl>
      </InlineHelp>

      <div className="grid gap-6 lg:grid-cols-2">
        <ChartCard title={t("productivity.chart.cycle")} desc={t("productivity.chart.cycleDesc")}>
          <StackedBars rows={o.series.cycle} series={cycleSeries} label={t("productivity.chart.cycle")} />
        </ChartCard>
        <ChartCard title={t("productivity.chart.flow")} desc={t("productivity.chart.flowDesc")}>
          <LineSeries rows={o.series.throughput} series={flowSeries} label={t("productivity.chart.flow")} />
        </ChartCard>
        <ChartCard title={t("productivity.chart.deploys")} desc={t("productivity.chart.deploysDesc")}>
          <DeployTimeline rows={o.deployments} from={o.from} to={o.to} label={t("productivity.chart.deploys")}
            legend={deploySeries} tableHead={[t("productivity.col.when"), t("productivity.col.repo")]} />
        </ChartCard>
        <ChartCard title={t("productivity.chart.size")} desc={t("productivity.chart.sizeDesc")}>
          <SizeBars rows={o.size_histogram}
            caption={(count, cycle) => t("productivity.chart.sizeCaption", { count, cycle })} />
        </ChartCard>
        {impl.available && (
          <ChartCard title={t("productivity.chart.implementation")} desc={t("productivity.chart.implementationDesc")}>
            <LineSeries rows={impl.series}
              series={[{ key: "rate", label: t("productivity.kpi.implementation"), color: "var(--color-primary)" }]}
              label={t("productivity.chart.implementation")} format={pct} />
          </ChartCard>
        )}
      </div>
    </div>
  );
}

// ─── tables ──────────────────────────────────────────────────────────

function PrTable({ days, filters, sort, onSort }: {
  days: ProductivityWindow; filters: ProductivityFilters; sort: Sort; onSort: (s: Sort) => void;
}) {
  const t = useT();
  const token = useToken();
  const query = useQuery({
    queryKey: ["productivity", "prs", days, sort, filters],
    queryFn: () => productivityApi.prs(token!, days, sort, filters),
    enabled: !!token,
  });
  return (
    <Card>
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <CardTitle>{t("productivity.prs.title")}</CardTitle>
          <CardDescription>{t("productivity.prs.desc")}</CardDescription>
        </div>
        <SegmentedControl
          label={t("productivity.prs.sort")}
          size="sm"
          value={sort}
          onValueChange={(v) => onSort(v as Sort)}
          segments={SORTS.map((s) => ({ value: s, label: t(`productivity.sort.${s}`) }))}
        />
      </CardHeader>
      <CardContent>
        <QueryState query={query} skeleton={3}>
          {({ prs }) => prs.length === 0 ? (
            <p className="text-sm text-[var(--color-muted-foreground)]">{t("productivity.emptyShort")}</p>
          ) : (
            <Table>
              <THead>
                <TR>
                  <TH>{t("productivity.col.pr")}</TH>
                  <TH>{t("productivity.col.author")}</TH>
                  <TH className="text-right">{t("productivity.coding")}</TH>
                  <TH className="text-right">{t("productivity.pickup")}</TH>
                  <TH className="text-right">{t("productivity.review")}</TH>
                  <TH className="text-right">{t("productivity.col.cycle")}</TH>
                  <TH className="text-right">{t("productivity.col.size")}</TH>
                </TR>
              </THead>
              <TBody>
                {prs.map((p) => (
                  <TR key={`${p.provider}:${p.repo}:${p.number}`}>
                    <TD className="max-w-[28rem]">
                      <div className="flex items-center gap-1.5">
                        {p.url ? (
                          <a href={p.url} target="_blank" rel="noopener noreferrer"
                            className="inline-flex min-w-0 items-center gap-1 rounded hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]">
                            <span className="truncate">{p.title || `#${p.number}`}</span>
                            <ExternalLinkIcon className="size-3 shrink-0 text-[var(--color-muted-foreground)]" aria-hidden />
                          </a>
                        ) : <span className="truncate">{p.title || `#${p.number}`}</span>}
                      </div>
                      <div className="text-xs text-[var(--color-muted-foreground)]">{p.repo} #{p.number} · {formatDate(p.merged_at)}</div>
                    </TD>
                    <TD>{p.author ?? "—"}</TD>
                    <TD className="text-right tabular-nums">{duration(p.coding)}</TD>
                    <TD className="text-right tabular-nums">{p.reviewed ? duration(p.pickup) : "—"}</TD>
                    <TD className="text-right tabular-nums">{p.reviewed ? duration(p.review) : "—"}</TD>
                    <TD className="text-right font-medium tabular-nums">{duration(p.cycle)}</TD>
                    <TD className="text-right tabular-nums">{lines(p.lines)}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </QueryState>
      </CardContent>
    </Card>
  );
}

function DeveloperTable({ days, filters }: { days: ProductivityWindow; filters: ProductivityFilters }) {
  const t = useT();
  const token = useToken();
  const query = useQuery({
    queryKey: ["productivity", "developers", days, filters.repo, filters.group, filters.target],
    queryFn: () => productivityApi.developers(token!, days, filters),
    enabled: !!token,
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("productivity.devs.title")}</CardTitle>
        <CardDescription>{t("productivity.devs.desc")}</CardDescription>
      </CardHeader>
      <CardContent>
        <QueryState query={query} skeleton={3}>
          {({ developers, min_sample }) => developers.length === 0 ? (
            <p className="text-sm text-[var(--color-muted-foreground)]">{t("productivity.emptyShort")}</p>
          ) : (
            <>
              <Table>
                <THead>
                  <TR>
                    <TH>{t("productivity.col.person")}</TH>
                    <TH className="text-right">{t("productivity.col.merged")}</TH>
                    <TH className="text-right">{t("productivity.col.lines")}</TH>
                    <TH className="text-right">{t("productivity.col.median")}</TH>
                    <TH className="text-right">{t("productivity.col.reviews")}</TH>
                    <TH className="text-right">{t("productivity.col.comments")}</TH>
                  </TR>
                </THead>
                <TBody>
                  {developers.map((d) => (
                    <TR key={d.key}>
                      <TD>{d.name}</TD>
                      <TD className="text-right tabular-nums">{d.prs_merged}</TD>
                      <TD className="text-right tabular-nums">{d.lines || "—"}</TD>
                      <TD className="text-right tabular-nums">{duration(d.cycle_p50)}</TD>
                      <TD className="text-right tabular-nums">{d.reviews_given}</TD>
                      <TD className="text-right tabular-nums">{d.comments_given}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
              <p className="mt-2 text-xs text-[var(--color-muted-foreground)]">{t("productivity.devs.smallSample", { n: min_sample })}</p>
            </>
          )}
        </QueryState>
      </CardContent>
    </Card>
  );
}
