// Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
// see LICENSING.md and ee/README.md in the repository root.

/**
 * Client for /api/analytics/productivity (src/ee/analytics/productivity_router.py).
 *
 * Kept beside the view rather than in lib/api.ts: the endpoints exist only
 * under the analytics licence, so the client belongs on the licensed side of
 * the boundary. Durations are seconds, rates are 0..1, dates are ISO strings.
 */

import { api } from "@/lib/api";

export const PRODUCTIVITY_WINDOWS = [15, 30, 90, 180] as const;
export type ProductivityWindow = (typeof PRODUCTIVITY_WINDOWS)[number];

export type Band = "elite" | "high" | "medium" | "low";

/** A headline number with the same number for the previous equal period. */
export type Figure = {
  value: number | null;
  previous: number | null;
  /** Relative change against `previous` (0.25 = +25 %); null when it cannot be said. */
  delta: number | null;
  n?: number;
  p75?: number | null;
  p90?: number | null;
  band?: Band | null;
};

export type ImplementationFigure =
  | { available: false }
  | (Figure & {
      available: true;
      implemented: number;
      unimplemented: number;
      dismissed: number;
      abandoned: number;
      series: Array<{ date: string; rate: number | null; implemented: number; unimplemented: number }>;
    });

export type ProductivityOverview = {
  days: number;
  bucket: "week" | "day";
  from: string;
  to: string;
  truncated: boolean;
  /** The previous period starts before the synced history does: deltas rest on partial data. */
  previous_incomplete: boolean;
  covered_from: string | null;
  kpis: {
    cycle_time: Figure;
    lead_time: Figure & { undeployed_merged: number };
    deploy_frequency: Figure & { total: number; per_day: number | null };
    change_failure_rate: Figure & { failed: number; settled: number; unsettled: number };
    time_to_recover: Figure;
    merged_prs: Figure;
    opened_prs: Figure;
    pr_size: Figure;
    bug_ratio: Figure;
    implementation_rate: ImplementationFigure;
  };
  breakdown: {
    coding: Figure;
    pickup: Figure;
    review: Figure;
    merged_without_review: number;
    not_enriched: number;
  };
  series: {
    cycle: Array<{ date: string; n: number; coding: number | null; pickup: number | null; review: number | null }>;
    throughput: Array<{ date: string; opened: number; merged: number; declined: number }>;
  };
  size_histogram: Array<{ bucket: string; max_lines: number | null; count: number; cycle_p50: number | null }>;
  deployments: Array<{ id: string; repo: string; at: string | null; failed: boolean; settled: boolean; source: string }>;
};

export type ProductivityPr = {
  provider: string;
  repo: string;
  number: number;
  title: string;
  url: string | null;
  author: string | null;
  target_branch: string | null;
  created_at: string | null;
  merged_at: string | null;
  coding: number | null;
  pickup: number | null;
  review: number | null;
  cycle: number | null;
  reviewed: boolean;
  lines: number | null;
  files: number | null;
  kind: string;
};

export type ProductivityDeveloper = {
  key: string;
  name: string;
  prs_merged: number;
  lines: number;
  cycle_p50: number | null;
  reviews_given: number;
  comments_given: number;
  small_sample: boolean;
};

export type ProductivityFilterOptions = {
  repos: Array<{ provider: string; repo: string }>;
  groups: string[];
  authors: Array<{ key: string; name: string }>;
  targets: string[];
};

export type ProductivityFilters = {
  repo?: string;
  group?: string;
  author?: string;
  target?: string;
};

export type SyncStatus = {
  provider: string;
  repo: string;
  watermark: string | null;
  backfill_from: string | null;
  backfill_done: boolean;
  last_run_at: string | null;
  last_ok_at: string | null;
  last_error: string | null;
  rate_limited_until: string | null;
  running: boolean;
  prs_total: number;
  prs_detailed: number;
  prs_pending: number;
};

export type SyncOverview = {
  repos: Array<{
    provider: string;
    repo: string;
    enabled: boolean;
    backfill_days: number;
    status: SyncStatus | null;
  }>;
  any_enabled: boolean;
  backfilling: boolean;
  rate_limited_until: string | null;
};

export type SettingValue = boolean | number | string | string[];
export type SettingsLayers = {
  workspace: Record<string, SettingValue>;
  repo: Record<string, SettingValue>;
  resolved: Record<string, SettingValue>;
  builtin: Record<string, SettingValue>;
  limits: { backfill_days_max: number; rate_per_hour: [number, number]; deploy_sources: string[] };
  queued?: number;
};

export type BackfillEstimate = {
  days: number;
  rate_per_hour: number;
  prs: number | null;
  requests: number | null;
  hours: number | null;
};

const BASE = "/api/analytics/productivity";

function query(params: Record<string, string | number | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const productivityApi = {
  overview: (token: string, days: number, f: ProductivityFilters) =>
    api<ProductivityOverview>(`${BASE}/overview${query({ days, ...f })}`, { token }),
  prs: (token: string, days: number, sort: string, f: ProductivityFilters) =>
    api<{ prs: ProductivityPr[]; truncated: boolean }>(
      `${BASE}/prs${query({ days, sort, limit: 25, ...f })}`, { token }),
  developers: (token: string, days: number, f: ProductivityFilters) =>
    api<{ developers: ProductivityDeveloper[]; min_sample: number; truncated: boolean }>(
      `${BASE}/developers${query({ days, repo: f.repo, group: f.group, target: f.target })}`, { token }),
  filters: (token: string) => api<ProductivityFilterOptions>(`${BASE}/filters`, { token }),
  sync: (token: string) => api<SyncOverview>(`${BASE}/sync`, { token }),
  runSync: (token: string, provider: string, repo: string, full = false) =>
    api<{ queued: boolean; detail: string | null }>(`${BASE}/sync/run`, {
      token, method: "POST", json: { provider, repo, full },
    }),
  estimate: (token: string, provider: string, repo: string, backfillDays?: number) =>
    api<BackfillEstimate>(
      `${BASE}/sync/estimate${query({ provider, repo, backfill_days: backfillDays })}`, { token }),
  settings: (token: string, scope?: { provider: string; repo: string }) =>
    api<SettingsLayers>(`${BASE}/settings${query({ ...scope })}`, { token }),
  saveSettings: (
    token: string, changes: Record<string, SettingValue | null>,
    scope?: { provider: string; repo: string },
  ) =>
    api<SettingsLayers>(`${BASE}/settings`, {
      token, method: "PUT", json: { ...scope, changes },
    }),
};
