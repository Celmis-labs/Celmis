"use client";

/**
 * /admin/usage — where the tokens and money actually went.
 *
 * Reads the llm_spend ledger: totals, a bucketed series, and every breakdown
 * the API offers — by surface (Q&A chat vs PR review vs embeddings), by review
 * agent, by repository, by operation, by model, by provider, by user and by
 * how the call is paid for. Also hosts the workspace budget cap.
 *
 * ONE breakdown at a time. All eight used to render at once, eight full tables
 * stacked two-up, and the page became a wall nobody could read: the reader had
 * to scroll past seven answers to reach the one they came for. The eight are
 * now a picker, the chosen one gets the full width, and the things that give a
 * number its meaning — the totals, the series, the active filters — stay above
 * it always, because those are context rather than content.
 *
 * The point of the page is still the drill-down: clicking any row pins that
 * value as a filter and BOTH reads re-run with it, so "PR review" → "which
 * repo" → "which agent" is three clicks rather than three hand-edited URLs.
 * Every filter the API accepts is reachable that way, and the active ones sit
 * above the fold as removable chips so nobody reads a filtered number as a
 * total.
 *
 * Cost honesty: OpenRouter reports the real charge; everything else is priced
 * from LiteLLM's table, so the page shows what share of the number is an
 * estimate rather than pretending it's exact. Subscription-billed calls
 * (Claude Code sessions) genuinely cost $0 per token, which is why the share
 * bars fall back to tokens when nothing in the window is metered — otherwise
 * a real workload would render as a row of empty bars.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { useCanManageWorkspace } from "@/lib/use-workspace-role";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  BotIcon, CoinsIcon, CpuIcon, DatabaseIcon, FilterIcon, FolderGitIcon,
  GaugeIcon, LayersIcon, SaveIcon, TrendingUpIcon, UsersIcon, WalletIcon,
  XIcon,
} from "lucide-react";

import {
  spendApi,
  type SpendBucket, type SpendDaily, type SpendGroupRow, type SpendQuery,
  type SpendSummary,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";

import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";

type Translate = (k: string, v?: Record<string, string | number>) => string;

/** The five dimensions the API can filter on. `provider`, `user` and
 *  `billing` are readable breakdowns but not filters — clicking them would
 *  send a parameter the backend ignores, and a filter that silently does
 *  nothing is worse than no filter. */
type FilterKey = "surface" | "repo" | "model" | "operation" | "agent";
type Filters = Partial<Record<FilterKey, string>>;

const FILTER_KEYS: FilterKey[] = ["surface", "repo", "model", "operation", "agent"];

const FILTER_LABEL: Record<FilterKey, string> = {
  surface: "admin.usage.filterSurface",
  repo: "admin.usage.filterRepo",
  model: "admin.usage.filterModel",
  operation: "admin.usage.filterOperation",
  agent: "admin.usage.filterAgent",
};

type RangePreset = "today" | "d7" | "d30" | "d90" | "month" | "custom";

const RANGE_PRESETS: RangePreset[] = ["today", "d7", "d30", "d90", "month", "custom"];

const BUCKETS: SpendBucket[] = ["hour", "day", "week", "month"];

const BUCKET_LABEL: Record<SpendBucket, string> = {
  hour: "admin.usage.bucketHour",
  day: "admin.usage.bucketDay",
  week: "admin.usage.bucketWeek",
  month: "admin.usage.bucketMonth",
};

/** What the series bars measure. Cost alone is a flat line for a workspace
 *  running on a Claude subscription, where the work is real and the price
 *  is zero. */
type Metric = "cost" | "tokens" | "calls";

const METRICS: Metric[] = ["cost", "tokens", "calls"];

const METRIC_LABEL: Record<Metric, string> = {
  cost: "admin.usage.metricCost",
  tokens: "admin.usage.metricTokens",
  calls: "admin.usage.metricCalls",
};

/**
 * The surface strings are internal, and two of them read as the same thing in
 * a list: `automation` is the Celmis planner turning a sentence into a plan,
 * `agent` is a Claude Code session. Anything NOT in this map still renders,
 * under its raw key — a surface added on the backend has to show up as a
 * number somebody can ask about, not vanish from the page.
 */
const SURFACE_LABEL: Record<string, { label: string; hint: string }> = {
  qa: { label: "admin.usage.surfaceQa", hint: "admin.usage.surfaceQaHint" },
  automation: {
    label: "admin.usage.surfaceAutomation", hint: "admin.usage.surfaceAutomationHint",
  },
  review: { label: "admin.usage.surfaceReview", hint: "admin.usage.surfaceReviewHint" },
  vault: { label: "admin.usage.surfaceVault", hint: "admin.usage.surfaceVaultHint" },
  agent: { label: "admin.usage.surfaceAgent", hint: "admin.usage.surfaceAgentHint" },
  deps: { label: "admin.usage.surfaceDeps", hint: "admin.usage.surfaceDepsHint" },
  embeddings: {
    label: "admin.usage.surfaceEmbeddings", hint: "admin.usage.surfaceEmbeddingsHint",
  },
  other: { label: "admin.usage.surfaceOther", hint: "admin.usage.surfaceOtherHint" },
};

const BILLING_LABEL: Record<string, string> = {
  subscription: "admin.usage.billingSubscription",
  api_key: "admin.usage.billingApiKey",
};

/** A null group key comes back from the API as this exact string. */
const NULL_KEY = "—";

/**
 * The eight cuts of the same window, as data rather than as eight hand-written
 * cards. One of them is on screen at a time; the rest are one click away.
 *
 * `filter` is set only where the dimension's name is also a filter the API
 * accepts — that is what makes rows in this breakdown clickable, and it is why
 * provider, user and billing are read-only tables.
 */
type Dimension =
  | "surface" | "repo" | "agent" | "operation"
  | "model" | "provider" | "user" | "billing";

type DimensionDef = {
  key: Dimension;
  /** The label this page already used as the card title. */
  title: string;
  description: string;
  icon: React.ReactNode;
  rows: (s: SpendSummary) => SpendGroupRow[];
  filter?: FilterKey;
  labelFor?: (key: string, t: Translate) => { label: string; hint?: string };
  /** What "—" means in THIS cut. Defaults to the repository explanation. */
  nullHint?: string;
};

const DIMENSIONS: DimensionDef[] = [
  {
    key: "surface", title: "admin.usage.bySurface", description: "admin.usage.bySurfaceDesc",
    icon: <DatabaseIcon className="h-4 w-4" />, rows: (s) => s.by_surface,
    filter: "surface", labelFor: (k, t) => surfaceRow(k, t),
    nullHint: "admin.usage.nullSurface",
  },
  {
    key: "repo", title: "admin.usage.byRepo", description: "admin.usage.byRepoDesc",
    icon: <FolderGitIcon className="h-4 w-4" />, rows: (s) => s.by_repo, filter: "repo",
  },
  {
    key: "agent", title: "admin.usage.byAgent", description: "admin.usage.byAgentDesc",
    icon: <BotIcon className="h-4 w-4" />, rows: (s) => s.by_agent, filter: "agent",
  },
  {
    key: "operation", title: "admin.usage.byOperation",
    description: "admin.usage.byOperationDesc",
    icon: <LayersIcon className="h-4 w-4" />, rows: (s) => s.by_operation,
    filter: "operation",
  },
  {
    key: "model", title: "admin.usage.byModel", description: "admin.usage.byModelDesc",
    icon: <CpuIcon className="h-4 w-4" />, rows: (s) => s.by_model, filter: "model",
  },
  {
    key: "provider", title: "admin.usage.byProvider",
    description: "admin.usage.byProviderDesc",
    icon: <CoinsIcon className="h-4 w-4" />, rows: (s) => s.by_provider,
  },
  {
    key: "user", title: "admin.usage.byUser", description: "admin.usage.byUserDesc",
    icon: <UsersIcon className="h-4 w-4" />, rows: (s) => s.by_user,
    nullHint: "admin.usage.nullUser",
  },
  {
    key: "billing", title: "admin.usage.byBilling", description: "admin.usage.byBillingDesc",
    icon: <WalletIcon className="h-4 w-4" />, rows: (s) => s.by_billing,
    labelFor: (k, t) => ({ label: BILLING_LABEL[k] ? t(BILLING_LABEL[k]) : k }),
  },
];

/** Surface first, deliberately: the first question anybody brings to this page
 *  is "where did the money go" at the coarsest useful grain — product area,
 *  not model name. Everything else is a follow-up to that answer. */
const DEFAULT_DIMENSION: Dimension = "surface";

const DIMENSION_KEYS: Dimension[] = DIMENSIONS.map((d) => d.key);

const fmtInt = (n: number) => new Intl.NumberFormat().format(Math.round(n));
const fmtUsd = (n: number) => `$${n.toFixed(n < 1 ? 4 : 2)}`;

/** Bars carry a label per bucket; 4-digit thousands separators would not fit
 *  and are not the point at that size. */
const fmtCompact = (n: number) =>
  new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 })
    .format(Math.round(n));

function startOfDayIso(d: Date): string {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  return x.toISOString();
}

/**
 * A preset becomes the window both reads share. `since`/`until` win over
 * `days` on the backend, so the presets that mean a calendar boundary
 * ("today", "this month") send an explicit start rather than a day count that
 * would drift with the clock.
 */
function windowFor(
  preset: RangePreset, customSince: string, customUntil: string,
): SpendQuery {
  const now = new Date();
  switch (preset) {
    case "today":
      return { days: 1, since: startOfDayIso(now) };
    case "d7":
      return { days: 7 };
    case "d90":
      return { days: 90 };
    case "month":
      return {
        days: 31,
        since: new Date(now.getFullYear(), now.getMonth(), 1).toISOString(),
      };
    case "custom": {
      const q: SpendQuery = { days: 30 };
      // Local midnight to local end-of-day: a person picking 1–3 August means
      // three whole days in their own timezone, not 00:00 UTC to 00:00 UTC.
      if (customSince) q.since = new Date(`${customSince}T00:00:00`).toISOString();
      if (customUntil) q.until = new Date(`${customUntil}T23:59:59`).toISOString();
      return q;
    }
    default:
      return { days: 30 };
  }
}

/** A bucket's timestamp, rendered at the granularity it actually has: the
 *  hour bucket is a full ISO timestamp, the rest are plain dates — and a
 *  plain date must NOT go through `new Date(iso)`, which reads it as UTC
 *  midnight and shows the previous day west of Greenwich. */
function bucketLabel(raw: string, bucket: SpendBucket): string {
  if (bucket === "hour") return formatDateTime(raw);
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
  if (!m) return raw;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  if (bucket === "month") {
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short" });
  }
  return d.toLocaleDateString(undefined, { day: "2-digit", month: "short" });
}

export default function UsagePage() {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();

  const [preset, setPreset] = useState<RangePreset>("d30");
  const [customSince, setCustomSince] = useState("");
  const [customUntil, setCustomUntil] = useState("");
  const [bucket, setBucket] = useState<SpendBucket>("day");
  const [metric, setMetric] = useState<Metric>("cost");
  const [filters, setFilters] = useState<Filters>({});
  // Which of the eight breakdowns is on screen.
  const [dimension, setDimension] = useState<Dimension>(DEFAULT_DIMENSION);
  // The server keeps a summary a summary by returning the top 50 of each
  // breakdown. That made a repository ranked 51st unreachable by any amount
  // of clicking — this is the way past it.
  const [showAll, setShowAll] = useState(false);
  // Reading what a workspace spent is its members' business — it is their
  // workspace's money. Setting the cap is a control over everyone else's
  // work, and follows the same rule as choosing the models: owner or admin
  // of THIS workspace, not a global admin somewhere.
  const canManage = useCanManageWorkspace();

  // ── the view lives in the URL ──────────────────────────────────────
  //
  // Three filters deep is a real piece of work, and it used to exist only in
  // this component: it could not be sent to a colleague, Back left the page
  // instead of undoing a step, and a refresh threw it away. The chosen
  // breakdown belongs in there with the rest of it — "look at the model
  // table for last month" is one link, and coming back lands on the cut you
  // were reading rather than on the default. Written with
  // history.replaceState rather than useSearchParams so the page needs no
  // Suspense boundary and renders identically on the server.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const p = q.get("range") as RangePreset | null;
    if (p && RANGE_PRESETS.includes(p)) setPreset(p);
    const b = q.get("bucket") as SpendBucket | null;
    if (b && BUCKETS.includes(b)) setBucket(b);
    const m = q.get("metric") as Metric | null;
    if (m && ["cost", "tokens", "calls"].includes(m)) setMetric(m);
    const d = q.get("by") as Dimension | null;
    if (d && DIMENSION_KEYS.includes(d)) setDimension(d);
    if (q.get("since")) setCustomSince(q.get("since")!);
    if (q.get("until")) setCustomUntil(q.get("until")!);
    if (q.get("all") === "1") setShowAll(true);
    const restored: Filters = {};
    for (const k of FILTER_KEYS) {
      const v = q.get(k);
      if (v) restored[k] = v;
    }
    if (Object.keys(restored).length) setFilters(restored);
  }, []);

  useEffect(() => {
    const q = new URLSearchParams();
    if (preset !== "d30") q.set("range", preset);
    if (bucket !== "day") q.set("bucket", bucket);
    if (metric !== "cost") q.set("metric", metric);
    if (dimension !== DEFAULT_DIMENSION) q.set("by", dimension);
    if (preset === "custom") {
      if (customSince) q.set("since", customSince);
      if (customUntil) q.set("until", customUntil);
    }
    if (showAll) q.set("all", "1");
    for (const k of FILTER_KEYS) if (filters[k]) q.set(k, filters[k]!);
    const search = q.toString();
    window.history.replaceState(
      null, "", search ? `${window.location.pathname}?${search}` : window.location.pathname,
    );
  }, [preset, bucket, metric, dimension, customSince, customUntil, showAll, filters]);

  const range = useMemo(
    () => windowFor(preset, customSince, customUntil),
    [preset, customSince, customUntil],
  );
  // One object drives both endpoints, so the series can never be answering a
  // different question from the tables above it.
  const query = useMemo<SpendQuery>(
    () => ({ ...range, ...filters, ...(showAll ? { limit: 500 } : {}) }),
    [range, filters, showAll],
  );

  const summary = useQuery({
    queryKey: ["spend", "summary", query],
    queryFn: () => spendApi.summary(token!, query),
    enabled: !!token,
  });
  const series = useQuery({
    queryKey: ["spend", "daily", query, bucket],
    queryFn: () => spendApi.daily(token!, query, bucket),
    enabled: !!token,
  });
  const budget = useQuery({
    queryKey: ["spend", "budget"],
    queryFn: () => spendApi.getBudget(token!),
    enabled: !!token,
  });

  const pickPreset = useCallback((p: RangePreset) => {
    setPreset(p);
    // One afternoon read as daily bars is a single bar. Nudge the bucket to
    // the granularity the range can actually show, but never override a
    // choice that still makes sense.
    setBucket((b) => (p === "today" ? "hour" : b === "hour" ? "day" : b));
  }, []);

  const toggleFilter = useCallback((key: FilterKey, value: string) => {
    setFilters((f) => {
      const next = { ...f };
      if (next[key] === value) delete next[key];
      else next[key] = value;
      return next;
    });
  }, []);

  const clearFilter = useCallback((key: FilterKey) => {
    setFilters((f) => {
      const next = { ...f };
      delete next[key];
      return next;
    });
  }, []);

  // A URL naming a dimension that a later build removed must not blank the
  // page out; fall back to the default rather than to nothing.
  const dim = useMemo(
    () => DIMENSIONS.find((d) => d.key === dimension) ?? DIMENSIONS[0],
    [dimension],
  );

  const s = summary.data;
  const activeFilters = FILTER_KEYS.filter((k) => filters[k]);
  const dimRows = s ? dim.rows(s) : [];

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<GaugeIcon className="h-6 w-6" />}
        title={t("admin.usage.title")}
        description={t("admin.usage.description")}
        actions={
          <div className="flex flex-wrap gap-1">
            {RANGE_PRESETS.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => pickPreset(p)}
                className={`rounded-md px-3 py-1.5 text-xs transition-colors ${
                  preset === p
                    ? "bg-[var(--color-brand-muted)] text-[var(--color-brand)] font-medium"
                    : "text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)]"
                }`}
              >
                {presetLabel(p, t)}
              </button>
            ))}
          </div>
        }
        tabs={<SectionTabs set="admin" />}
      />

      {/* Controls: the custom window, the series granularity, and whatever is
          currently narrowing the page. */}
      <Card>
        <CardContent className="space-y-3 pt-6">
          <div className="flex flex-wrap items-end gap-3">
            {preset === "custom" && (
              <>
                <div className="space-y-1">
                  <Label htmlFor="usage-since">{t("admin.usage.since")}</Label>
                  <Input
                    id="usage-since" type="date" className="w-44"
                    value={customSince}
                    onChange={(e) => setCustomSince(e.target.value)}
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="usage-until">{t("admin.usage.until")}</Label>
                  <Input
                    id="usage-until" type="date" className="w-44"
                    value={customUntil}
                    onChange={(e) => setCustomUntil(e.target.value)}
                  />
                </div>
              </>
            )}
            <div className="space-y-1">
              <Label htmlFor="usage-bucket">{t("admin.usage.bucket")}</Label>
              <Select
                id="usage-bucket"
                className="w-44"
                value={bucket}
                onChange={(v) => setBucket(v as SpendBucket)}
                options={BUCKETS.map((b) => ({ value: b, label: t(BUCKET_LABEL[b]) }))}
              />
            </div>
            <div className="ml-auto text-right text-[11px] text-[var(--color-muted-foreground)]">
              {s && (
                <>
                  <div>{t("admin.usage.windowLabel")}</div>
                  <div className="tabular-nums">
                    {formatDateTime(s.since)} → {formatDateTime(s.until)}
                  </div>
                </>
              )}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 border-t border-[var(--color-border)] pt-3">
            <FilterIcon className="h-3.5 w-3.5 text-[var(--color-muted-foreground)]" />
            {activeFilters.length === 0 ? (
              <span className="text-xs text-[var(--color-muted-foreground)]">
                {t("admin.usage.filtersEmpty")}
              </span>
            ) : (
              <>
                {activeFilters.map((k) => (
                  <Badge key={k} variant="brand" className="gap-1.5 py-1 font-normal">
                    <span className="text-[10px] uppercase tracking-wide opacity-70">
                      {t(FILTER_LABEL[k])}
                    </span>
                    <span className="font-medium">
                      {k === "surface" ? surfaceLabel(filters[k]!, t) : filters[k]}
                    </span>
                    <button
                      type="button"
                      onClick={() => clearFilter(k)}
                      aria-label={t("admin.usage.removeFilter", { name: t(FILTER_LABEL[k]) })}
                      className="rounded hover:opacity-70"
                    >
                      <XIcon className="h-3 w-3" />
                    </button>
                  </Badge>
                ))}
                <Button size="sm" variant="ghost" onClick={() => setFilters({})}>
                  {t("admin.usage.clearFilters")}
                </Button>
              </>
            )}
          </div>

          {/* The table shows the top 50 of the chosen dimension. That is the
              right default for reading and the wrong one for hunting: a
              repository ranked 51st could not be clicked into existence at
              all. */}
          <button
            type="button"
            onClick={() => setShowAll((v) => !v)}
            className="mt-2 text-xs text-[var(--color-muted-foreground)]
                       underline-offset-2 hover:underline"
          >
            {showAll ? t("admin.usage.showTop") : t("admin.usage.showAll")}
          </button>
        </CardContent>
      </Card>

      {summary.isLoading && (
        <div className="text-sm text-[var(--color-muted-foreground)]">{t("common.loading")}</div>
      )}

      {s && (
        <>
          {/* Totals */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard label={t("admin.usage.totalCost")} value={fmtUsd(s.cost_usd)}
              hint={s.estimated_share_pct > 0
                ? t("admin.usage.estimatedShare", { pct: s.estimated_share_pct })
                : t("admin.usage.actualCost")} />
            <StatCard label={t("admin.usage.calls")} value={fmtInt(s.calls)}
              hint={s.subscription_calls > 0
                ? t("admin.usage.subscriptionCalls", { n: fmtInt(s.subscription_calls) })
                : undefined} />
            <StatCard label={t("admin.usage.tokensIn")} value={fmtInt(s.tokens_in)}
              hint={t("admin.usage.cachedShare", { pct: s.cache_hit_pct })} />
            <StatCard label={t("admin.usage.tokensOut")} value={fmtInt(s.tokens_out)}
              hint={s.cached_tokens_in > 0
                ? t("admin.usage.cachedTotal", { cached: fmtInt(s.cached_tokens_in) })
                : undefined} />
          </div>

          <Card>
            <CardHeader>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <CardTitle className="flex items-center gap-2 text-base">
                    <TrendingUpIcon className="h-4 w-4" /> {t("admin.usage.trendTitle")}
                  </CardTitle>
                  <CardDescription>{t("admin.usage.trendDesc")}</CardDescription>
                </div>
                <div className="flex gap-1">
                  {METRICS.map((m) => (
                    <button
                      key={m}
                      type="button"
                      onClick={() => setMetric(m)}
                      className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                        metric === m
                          ? "bg-[var(--color-brand-muted)] text-[var(--color-brand)] font-medium"
                          : "text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)]"
                      }`}
                    >
                      {t(METRIC_LABEL[m])}
                    </button>
                  ))}
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <TrendChart
                points={series.data ?? []} bucket={bucket} metric={metric}
                loading={series.isLoading} t={t}
              />
            </CardContent>
          </Card>

          {s.calls === 0 && (
            <Card>
              <CardContent className="py-10 text-center text-sm text-[var(--color-muted-foreground)]">
                {t("admin.usage.noData")}
              </CardContent>
            </Card>
          )}

          {/* The one breakdown the reader asked for, full width. */}
          <Card>
            <CardHeader className="gap-3">
              {/* Wide: all eight visible, so the reader can see what the page
                  can answer without opening anything. Narrow: the same eight
                  as a select, because a row of eight chips on a phone is the
                  wall this change exists to remove. */}
              <div
                role="group"
                aria-label={t("admin.usage.dimension")}
                className="hidden flex-wrap gap-1 rounded-lg border border-[var(--color-border)]
                           bg-[var(--color-muted)]/40 p-1 sm:flex"
              >
                {DIMENSIONS.map((d) => {
                  const on = d.key === dim.key;
                  const filtered = Boolean(d.filter && filters[d.filter]);
                  return (
                    <button
                      key={d.key}
                      type="button"
                      aria-pressed={on}
                      onClick={() => setDimension(d.key)}
                      className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs
                                  transition-colors focus:outline-none focus-visible:ring-2
                                  focus-visible:ring-[var(--color-brand)] ${
                        on
                          ? "bg-[var(--color-card)] text-[var(--color-brand)] font-medium shadow-sm"
                          : "text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]"
                      }`}
                    >
                      {d.icon}
                      {t(d.title)}
                      {/* This cut is narrowing the page even while you are
                          reading another one. */}
                      {filtered && (
                        <span aria-hidden
                              className="h-1.5 w-1.5 rounded-full bg-[var(--color-brand)]" />
                      )}
                    </button>
                  );
                })}
              </div>
              <div className="space-y-1 sm:hidden">
                <Label htmlFor="usage-dimension">{t("admin.usage.dimension")}</Label>
                <Select
                  id="usage-dimension"
                  className="w-full"
                  value={dim.key}
                  onChange={(v) => setDimension(v as Dimension)}
                  options={DIMENSIONS.map((d) => ({
                    value: d.key,
                    label: t(d.title),
                    hint: fmtInt(d.rows(s).length),
                  }))}
                />
              </div>

              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <CardTitle className="flex items-center gap-2 text-base">
                    {dim.icon} {t(dim.title)}
                  </CardTitle>
                  <CardDescription>
                    {t(dim.description)}
                    {dim.filter && <> {t("admin.usage.clickToFilter")}</>}
                  </CardDescription>
                </div>
                {dimRows.length > 0 && (
                  <span className="shrink-0 text-[11px] tabular-nums text-[var(--color-muted-foreground)]">
                    {t("admin.usage.rowCount", { n: fmtInt(dimRows.length) })}
                  </span>
                )}
              </div>
            </CardHeader>
            <CardContent>
              <BreakdownTable
                rows={dimRows}
                summary={s}
                t={t}
                filterKey={dim.filter}
                active={dim.filter ? filters[dim.filter] : undefined}
                onPick={dim.filter ? toggleFilter : undefined}
                labelFor={dim.labelFor ? (k) => dim.labelFor!(k, t) : undefined}
                nullHintKey={dim.nullHint}
              />
            </CardContent>
          </Card>
        </>
      )}

      {/* Reading spend is a member's business — it is their workspace's
          money. Setting the cap is not: that is a control over everyone
          else's work, so this one card keeps the gate the whole page used
          to carry. */}
      {budget.data && canManage && (
        <BudgetCard onSaved={() => qc.invalidateQueries({ queryKey: ["spend"] })} />
      )}
    </PageShell>
  );
}

function presetLabel(p: RangePreset, t: Translate): string {
  switch (p) {
    case "today": return t("admin.usage.rangeToday");
    case "d7": return t("admin.usage.rangeDays", { days: 7 });
    case "d30": return t("admin.usage.rangeDays", { days: 30 });
    case "d90": return t("admin.usage.rangeDays", { days: 90 });
    case "month": return t("admin.usage.rangeMonth");
    default: return t("admin.usage.rangeCustom");
  }
}

/** Label only — for the filter chip, where the hint has no room. */
function surfaceLabel(key: string, t: Translate): string {
  const known = SURFACE_LABEL[key];
  return known ? t(known.label) : key;
}

/** Label + one line of what the surface actually is. An unknown key keeps its
 *  raw string and simply has nothing to explain. */
function surfaceRow(key: string, t: Translate): { label: string; hint?: string } {
  const known = SURFACE_LABEL[key];
  if (!known) return { label: key };
  return { label: t(known.label), hint: t(known.hint) };
}

function StatCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card>
      <CardContent className="pt-6">
        <div className="text-xs text-[var(--color-muted-foreground)]">{label}</div>
        <div className="mt-1 text-2xl font-semibold tracking-tight">{value}</div>
        {hint && <div className="mt-1 text-[11px] text-[var(--color-muted-foreground)]">{hint}</div>}
      </CardContent>
    </Card>
  );
}

function metricValue(p: SpendDaily, metric: Metric): number {
  if (metric === "cost") return p.cost_usd;
  if (metric === "calls") return p.calls;
  return p.tokens_in + p.tokens_out;
}

function TrendChart({
  points, bucket, metric, loading, t,
}: {
  points: SpendDaily[]; bucket: SpendBucket; metric: Metric;
  loading: boolean; t: Translate;
}) {
  if (loading) {
    return (
      <div className="h-40 animate-pulse rounded-md bg-[var(--color-muted)]" />
    );
  }
  if (!points.length) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-[var(--color-muted-foreground)]">
        {t("admin.usage.trendEmpty")}
      </div>
    );
  }
  const max = Math.max(1, ...points.map((p) => metricValue(p, metric)));
  const fmt = (p: SpendDaily) =>
    metric === "cost" ? fmtUsd(p.cost_usd) : fmtCompact(metricValue(p, metric));
  return (
    <div className="space-y-1">
      <div className="overflow-x-auto">
        <div className="flex h-40 min-w-full items-stretch gap-px">
          {points.map((p) => {
            const v = metricValue(p, metric);
            const pct = (v / max) * 100;
            return (
              <div
                key={p.date}
                className="flex h-full min-w-[3px] flex-1 items-end"
                title={t("admin.usage.pointTooltip", {
                  date: bucketLabel(p.date, bucket),
                  value: fmt(p),
                  calls: fmtInt(p.calls),
                  tin: fmtInt(p.tokens_in),
                  tout: fmtInt(p.tokens_out),
                })}
              >
                <div
                  className="w-full rounded-t-sm bg-[var(--color-brand)]/70 transition-colors hover:bg-[var(--color-brand)]"
                  style={{ height: `${v > 0 ? Math.max(pct, 2) : 0}%` }}
                />
              </div>
            );
          })}
        </div>
      </div>
      <div className="flex justify-between text-[10px] tabular-nums text-[var(--color-muted-foreground)]">
        <span>{bucketLabel(points[0].date, bucket)}</span>
        <span>
          {t("admin.usage.trendPeak", { value: fmt(
            points.reduce((a, b) => (metricValue(b, metric) > metricValue(a, metric) ? b : a)),
          ) })}
        </span>
        <span>{bucketLabel(points[points.length - 1].date, bucket)}</span>
      </div>
    </div>
  );
}

/**
 * One breakdown as a full table: calls, tokens in/out, cached, cost and the
 * share of the window — a column of numbers with no denominator makes the
 * reader do the arithmetic the page could have done.
 *
 * Share is measured against cost while anything in the window is metered, and
 * falls back to tokens (then calls) when nothing is — a subscription-only
 * workspace would otherwise see a table of 0% bars.
 *
 * Empty says so out loud. While all eight rendered at once an empty one could
 * simply not draw itself and the page still read correctly; now it is the only
 * thing on screen, and a card with a heading and a void under it looks broken
 * rather than answered.
 */
function BreakdownTable({
  rows, summary, t, filterKey, active, onPick, labelFor, nullHintKey,
}: {
  rows: SpendGroupRow[]; summary: SpendSummary; t: Translate;
  filterKey?: FilterKey;
  active?: string;
  onPick?: (key: FilterKey, value: string) => void;
  labelFor?: (key: string) => { label: string; hint?: string };
  nullHintKey?: string;
}) {
  if (!rows.length) {
    return (
      <p className="py-10 text-center text-sm text-[var(--color-muted-foreground)]">
        {t("admin.usage.breakdownEmpty")}
      </p>
    );
  }

  const totalTokens = summary.tokens_in + summary.tokens_out;
  const basis: Metric =
    summary.cost_usd > 0 ? "cost" : totalTokens > 0 ? "tokens" : "calls";
  const denom =
    basis === "cost" ? summary.cost_usd : basis === "tokens" ? totalTokens : summary.calls;
  const shareOf = (r: SpendGroupRow) => {
    if (denom <= 0) return 0;
    const v = basis === "cost"
      ? r.cost_usd
      : basis === "tokens" ? r.tokens_in + r.tokens_out : r.calls;
    return (v / denom) * 100;
  };

  const hasNullRow = rows.some((r) => r.key === NULL_KEY);

  return (
    <>
      <Table className="text-sm">
        <THead>
          <TR>
            <TH>{t("admin.usage.colName")}</TH>
            <TH className="text-right">{t("admin.usage.colCalls")}</TH>
            <TH className="text-right">{t("admin.usage.colTokensIn")}</TH>
            <TH className="text-right">{t("admin.usage.colTokensOut")}</TH>
            <TH className="text-right">{t("admin.usage.colCached")}</TH>
            <TH className="text-right">{t("admin.usage.colCost")}</TH>
            <TH className="text-right" title={t(
              basis === "cost" ? "admin.usage.shareOfCost"
                : basis === "tokens" ? "admin.usage.shareOfTokens"
                : "admin.usage.shareOfCalls",
            )}>
              {t("admin.usage.colShare")}
            </TH>
          </TR>
        </THead>
        <TBody>
          {rows.map((r) => {
            // A "—" row is a NULL in the ledger, not a value: filtering by
            // the literal em dash would match nothing at all.
            const clickable = Boolean(filterKey && onPick) && r.key !== NULL_KEY;
            const isActive = active === r.key;
            // A label the server resolved wins over the raw key: a uuid in a
            // column headed "Who asked" answers nobody's question, and two
            // uuids cannot even be told apart as two people.
            const meta = labelFor?.(r.key) ?? { label: r.label || r.key };
            const pct = shareOf(r);
            const pick = () => filterKey && onPick?.(filterKey, r.key);
            return (
              <TR
                key={r.key}
                onClick={clickable ? pick : undefined}
                className={`${clickable ? "cursor-pointer" : ""} ${
                  isActive ? "bg-[var(--color-brand-muted)]/50" : ""
                }`}
              >
                <TD className="max-w-[28rem] py-2.5">
                  <div className="flex items-center gap-1.5">
                    {clickable ? (
                      <button
                        type="button"
                        aria-pressed={isActive}
                        onClick={(e) => { e.stopPropagation(); pick(); }}
                        className="truncate rounded text-left font-medium hover:text-[var(--color-brand)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-brand)]"
                      >
                        {meta.label}
                      </button>
                    ) : (
                      <span className="truncate font-medium">{meta.label}</span>
                    )}
                    {isActive && (
                      <Badge variant="brand" className="shrink-0 text-[9px]">
                        {t("admin.usage.filterActive")}
                      </Badge>
                    )}
                  </div>
                  {meta.hint && (
                    <div className="mt-0.5 text-xs text-[var(--color-muted-foreground)]">
                      {meta.hint}
                    </div>
                  )}
                  <div className="mt-1.5 h-1.5 w-full max-w-[20rem] overflow-hidden rounded-full bg-[var(--color-muted)]">
                    <div className="h-full rounded-full bg-[var(--color-brand)]"
                         style={{ width: `${Math.min(Math.max(pct, 1), 100)}%` }} />
                  </div>
                </TD>
                <TD className="py-2.5 text-right tabular-nums">{fmtInt(r.calls)}</TD>
                <TD className="py-2.5 text-right tabular-nums">{fmtInt(r.tokens_in)}</TD>
                <TD className="py-2.5 text-right tabular-nums">{fmtInt(r.tokens_out)}</TD>
                <TD className="py-2.5 text-right tabular-nums text-[var(--color-muted-foreground)]">
                  {r.cached_tokens_in > 0 ? fmtInt(r.cached_tokens_in) : "—"}
                </TD>
                <TD className="py-2.5 text-right font-medium tabular-nums">{fmtUsd(r.cost_usd)}</TD>
                <TD className="py-2.5 text-right font-medium tabular-nums">
                  {pct < 10 ? pct.toFixed(1) : pct.toFixed(0)}%
                </TD>
              </TR>
            );
          })}
        </TBody>
      </Table>
      {hasNullRow && (
        // The explanation has to match the column. It said "chat and
        // embeddings span the workspace rather than one repository" under
        // every breakdown, including "By user", where the empty row means
        // nobody was recorded — a different fact entirely.
        <p className="mt-2 text-[11px] text-[var(--color-muted-foreground)]">
          {t(nullHintKey ?? "admin.usage.nullRowHint")}
        </p>
      )}
    </>
  );
}

function BudgetCard({ onSaved }: { onSaved: () => void }) {
  const token = useToken();
  const t = useT();
  const budget = useQuery({
    queryKey: ["spend", "budget"],
    queryFn: () => spendApi.getBudget(token!),
    enabled: !!token,
  });
  const [cap, setCap] = useState<string | null>(null);
  const [alertPct, setAlertPct] = useState<string | null>(null);
  const [hardStop, setHardStop] = useState<boolean | null>(null);

  const b = budget.data;
  const capVal = cap ?? String(b?.cap_usd ?? 0);
  const alertVal = alertPct ?? String(b?.alert_pct ?? 80);
  const hardVal = hardStop ?? Boolean(b?.hard_stop);

  const save = useMutation({
    mutationFn: () => spendApi.saveBudget(token!, {
      monthly_usd_cap: Number(capVal), alert_pct: Number(alertVal), hard_stop: hardVal,
    }),
    onSuccess: () => { toast.success(t("admin.usage.budgetSaved")); onSaved(); },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <GaugeIcon className="h-4 w-4" /> {t("admin.usage.budgetTitle")}
        </CardTitle>
        <CardDescription>{t("admin.usage.budgetDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {b?.enabled && (
          <div className="space-y-1">
            <div className="flex justify-between text-xs">
              <span className="text-[var(--color-muted-foreground)]">
                {t("admin.usage.monthToDate")}
              </span>
              <span className="tabular-nums">
                {fmtUsd(b.spent_usd)} / {fmtUsd(b.cap_usd)} ({b.used_pct}%)
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--color-muted)]">
              <div
                className={`h-full rounded-full ${b.over_cap ? "bg-red-500" : b.over_alert ? "bg-amber-500" : "bg-[var(--color-brand)]"}`}
                style={{ width: `${Math.min(b.used_pct, 100)}%` }}
              />
            </div>
            {b.over_cap && (
              <p className="text-[11px] text-red-600 dark:text-red-400">
                {b.hard_stop ? t("admin.usage.overCapBlocked") : t("admin.usage.overCapWarn")}
              </p>
            )}
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] items-end">
          <div>
            <Label>{t("admin.usage.capLabel")}</Label>
            <Input type="number" min={0} step="1" value={capVal}
                   onChange={(e) => setCap(e.target.value)} />
            <p className="mt-1 text-[11px] text-[var(--color-muted-foreground)]">
              {t("admin.usage.capHint")}
            </p>
          </div>
          <div>
            <Label>{t("admin.usage.alertLabel")}</Label>
            <Input type="number" min={1} max={100} value={alertVal}
                   onChange={(e) => setAlertPct(e.target.value)} />
          </div>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            <SaveIcon className="h-4 w-4 mr-1" />
            {save.isPending ? t("common.saving") : t("common.save")}
          </Button>
        </div>

        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={hardVal} onChange={(e) => setHardStop(e.target.checked)} />
          {t("admin.usage.hardStopLabel")}
          <Badge variant="outline" className="text-[9px]">{t("admin.usage.hardStopHint")}</Badge>
        </label>
      </CardContent>
    </Card>
  );
}
