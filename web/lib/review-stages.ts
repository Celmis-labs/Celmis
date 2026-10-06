/**
 * The stages of a review run, as the timeline shows them.
 *
 * Pure functions, no React — so the decisions the page makes (how a duration
 * reads, which pill a status gets, which label a stage key gets, how long a
 * run took when it never recorded an elapsed time) are compiled and checked
 * on their own, the way tests/web checks the other page logic.
 */

export type StageStatus = "success" | "skipped" | "failed" | "running";

export type ReviewStage = {
  key: string;
  name: string;
  status: StageStatus | string;
  started_at: string | null;
  duration_ms: number | null;
  reason: string;
  meta?: Record<string, string | number | boolean> | null;
};

/** The badge variant for a stage pill. An unknown word is a failure, never
 *  a success — the same rule the server applies when it reads a row back. */
export function stagePillVariant(
  status: string | null | undefined,
): "success" | "default" | "destructive" | "brand" {
  switch (status) {
    case "success":
      return "success";
    case "skipped":
      return "default";
    case "running":
      return "brand";
    default:
      return "destructive";
  }
}

/** The status vocabulary the page has words for; anything else reads as failed. */
export function stageStatusWord(status: string | null | undefined): StageStatus {
  return status === "success" || status === "skipped" || status === "running"
    ? status
    : "failed";
}

/** "850 ms", "12s", "3m 18s", "1h 02m" — "—" when unknown. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  if (m < 60) return `${m}m ${rest}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

/** How long a run took: its elapsed seconds, else finished − started, else
 *  the "finished" stage's span. null while it has no end. */
export function runDurationMs(run: {
  elapsed_seconds?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  stages?: ReviewStage[] | null;
}): number | null {
  if (typeof run.elapsed_seconds === "number" && Number.isFinite(run.elapsed_seconds)) {
    return Math.max(0, run.elapsed_seconds * 1000);
  }
  if (run.started_at && run.finished_at) {
    const a = Date.parse(run.started_at);
    const b = Date.parse(run.finished_at);
    if (Number.isFinite(a) && Number.isFinite(b)) return Math.max(0, b - a);
  }
  const fin = (run.stages ?? []).find((s) => s.key === "finished");
  return fin && typeof fin.duration_ms === "number" ? fin.duration_ms : null;
}

/** The i18n key for a stage's label. Agents share one label with the name
 *  filled in; an unknown key has none, and the page shows the server's name. */
export function stageLabel(stage: Pick<ReviewStage, "key" | "name">): {
  key: string | null;
  vars?: Record<string, string>;
} {
  if (stage.key.startsWith("agent:")) {
    return { key: "prs.stage.agent", vars: { name: stage.key.slice("agent:".length) } };
  }
  return KNOWN_STAGES.includes(stage.key) ? { key: `prs.stage.${stage.key}` } : { key: null };
}

export const KNOWN_STAGES = [
  "received", "queued", "retry", "fetch_pr", "settings", "ignore_globs",
  "gate_enabled", "gate_target_branch", "gate_draft", "gate_title", "gate_cadence", "scope",
  "context", "gate_size", "gate_hunks", "summary", "verifier", "learned_filter", "breaking_change", "compliance",
  "requirements", "resolve_issues", "publish", "record", "finished",
];

/** "3 days ago" / "5 minutes ago" in the reader's language, via Intl. */
export function relativeTime(
  iso: string | null | undefined, locale: string, now: number = Date.now(),
): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const diff = (t - now) / 1000;
  const abs = Math.abs(diff);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 31536000], ["month", 2592000], ["week", 604800],
    ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1],
  ];
  for (const [unit, secs] of units) {
    if (abs >= secs || unit === "second") {
      const value = Math.round(diff / secs);
      try {
        return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(value, unit);
      } catch {
        return new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(value, unit);
      }
    }
  }
  return iso;
}

/** The meta keys the page has a label for, in display order. */
export const META_KEYS = ["model", "findings", "tokens_in", "tokens_out", "turns",
  "kept", "vetoed", "files", "bytes", "limit", "target_branch", "trigger"];

/** A stage's meta as [key, value] pairs in a stable order — model first,
 *  then counts — with empty values dropped and numbers grouped. */
export function metaEntries(meta: ReviewStage["meta"]): [string, string][] {
  if (!meta) return [];
  const keys = [...META_KEYS.filter((k) => k in meta),
    ...Object.keys(meta).filter((k) => !META_KEYS.includes(k))];
  const out: [string, string][] = [];
  for (const k of keys) {
    const v = meta[k];
    if (v === null || v === undefined || v === "") continue;
    out.push([k, typeof v === "number" ? v.toLocaleString("en") : String(v)]);
  }
  return out;
}
