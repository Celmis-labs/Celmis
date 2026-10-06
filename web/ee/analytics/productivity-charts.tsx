"use client";

// Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
// see LICENSING.md and ee/README.md in the repository root.

/**
 * Charts for /productivity: plain SVG and CSS, no chart library, in the style
 * of the review analytics charts. Marks are drawn stretched (preserveAspectRatio
 * "none") so they fill the card at any width; every label is HTML beside the
 * SVG, so text is never distorted. Each chart names its series in a legend
 * (colour is never the only carrier: the legend, the hover title and the table
 * view all spell the numbers out) and ends in a collapsible table.
 */

import { useT } from "@/lib/i18n";
import { formatDate, formatDateTime } from "@/lib/format";

/** Seconds as the shortest honest unit: 42m, 13.5h, 3.2d. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

export type Series = { key: string; label: string; color: string };

const W = 600;
const H = 140;

function Legend({ series }: { series: Series[] }) {
  return (
    <ul className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--color-muted-foreground)]">
      {series.map((s) => (
        <li key={s.key} className="flex items-center gap-1.5">
          <span aria-hidden className="inline-block size-2.5 shrink-0 rounded-sm" style={{ background: s.color }} />
          {s.label}
        </li>
      ))}
    </ul>
  );
}

function Gridlines() {
  return (
    <>
      {[0.25, 0.5, 0.75].map((r) => (
        <line key={r} x1={0} x2={W} y1={H - r * (H - 8)} y2={H - r * (H - 8)}
          stroke="var(--color-border)" strokeWidth={1} strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />
      ))}
      <line x1={0} x2={W} y1={H} y2={H} stroke="var(--color-border-strong)" strokeWidth={1}
        vectorEffect="non-scaling-stroke" />
    </>
  );
}

function Ends({ first, last, max }: { first?: string; last?: string; max: string }) {
  return (
    <div className="mt-1 flex justify-between text-xs tabular-nums text-[var(--color-muted-foreground)]">
      <span>{formatDate(first)}</span>
      <span>{max}</span>
      <span>{formatDate(last)}</span>
    </div>
  );
}

function TableView({ head, rows }: { head: string[]; rows: Array<Array<string | number>> }) {
  // Index keys: a first column of formatted dates or times can repeat.
  const t = useT();
  return (
    <details className="mt-2 text-xs">
      <summary className="w-fit cursor-pointer rounded text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]">
        {t("analytics.tableView")}
      </summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="text-left text-[var(--color-muted-foreground)]">
              {head.map((h, i) => (
                <th key={h} className={`py-0.5 font-medium ${i ? "text-right" : ""}`}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>
                {r.map((c, i) => (
                  <td key={i} className={`py-0.5 tabular-nums ${i ? "text-right" : ""}`}>{c}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/** One column per bucket, its series stacked bottom to top. Values are durations. */
export function StackedBars({ rows, series, label }: {
  rows: Array<{ date: string } & Record<string, number | string | null>>;
  series: Series[];
  label: string;
}) {
  const total = (r: Record<string, number | string | null>) =>
    series.reduce((a, s) => a + (typeof r[s.key] === "number" ? (r[s.key] as number) : 0), 0);
  const max = Math.max(1, ...rows.map(total));
  const n = Math.max(1, rows.length);
  const gap = n > 40 ? 1 : 4;
  const bw = Math.max(1, W / n - gap);
  return (
    <div>
      <Legend series={series} />
      <svg viewBox={`0 0 ${W} ${H}`} className="h-40 w-full" role="img" aria-label={label} preserveAspectRatio="none">
        <Gridlines />
        {rows.map((r, i) => {
          let y = H;
          const x = i * (bw + gap);
          const title = `${formatDate(r.date)}: ${series.map((s) => `${s.label} ${duration(r[s.key] as number | null)}`).join(" · ")}`;
          return (
            <g key={r.date}>
              <rect x={x} y={0} width={bw + gap} height={H} fill="transparent"><title>{title}</title></rect>
              {series.map((s) => {
                const v = typeof r[s.key] === "number" ? (r[s.key] as number) : 0;
                if (v <= 0) return null;
                const h = (v / max) * (H - 8);
                y -= h;
                return (
                  <rect key={s.key} x={x} y={y} width={bw} height={h} fill={s.color} fillOpacity={0.9}
                    stroke="var(--color-card)" strokeWidth={1} vectorEffect="non-scaling-stroke" pointerEvents="none" />
                );
              })}
            </g>
          );
        })}
      </svg>
      <Ends first={rows[0]?.date} last={rows[rows.length - 1]?.date} max={duration(max)} />
      <TableView
        head={["", ...series.map((s) => s.label)]}
        rows={rows.filter((r) => total(r) > 0).map((r) => [
          formatDate(r.date), ...series.map((s) => duration(r[s.key] as number | null)),
        ])}
      />
    </div>
  );
}

/** One polyline per series; a null breaks the line instead of reading as zero. */
export function LineSeries({ rows, series, label, format = (v) => String(v) }: {
  rows: Array<{ date: string } & Record<string, number | string | null>>;
  series: Series[];
  label: string;
  format?: (v: number) => string;
}) {
  const values = rows.flatMap((r) => series.map((s) => r[s.key]).filter((v): v is number => typeof v === "number"));
  const max = Math.max(1e-9, ...values);
  const n = Math.max(2, rows.length);
  const px = (i: number) => (i / (n - 1)) * (W - 8) + 4;
  const py = (v: number) => H - 6 - (v / max) * (H - 18);
  return (
    <div>
      {series.length > 1 && <Legend series={series} />}
      <svg viewBox={`0 0 ${W} ${H}`} className="h-40 w-full" role="img" aria-label={label} preserveAspectRatio="none">
        <Gridlines />
        {series.map((s) => {
          const segments: string[] = [];
          let current = "";
          rows.forEach((r, i) => {
            const v = r[s.key];
            if (typeof v !== "number") {
              if (current) segments.push(current);
              current = "";
              return;
            }
            current += `${current ? "L" : "M"}${px(i).toFixed(1)} ${py(v).toFixed(1)}`;
          });
          if (current) segments.push(current);
          return (
            <path key={s.key} d={segments.join("")} fill="none" stroke={s.color} strokeWidth={2}
              strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
          );
        })}
        {rows.map((r, i) => (
          <rect key={r.date} x={px(i) - W / n / 2} y={0} width={W / n} height={H} fill="transparent">
            <title>{`${formatDate(r.date)}: ${series.map((s) => `${s.label} ${typeof r[s.key] === "number" ? format(r[s.key] as number) : "—"}`).join(" · ")}`}</title>
          </rect>
        ))}
      </svg>
      <Ends first={rows[0]?.date} last={rows[rows.length - 1]?.date} max={format(max)} />
      <TableView
        head={["", ...series.map((s) => s.label)]}
        rows={rows.map((r) => [
          formatDate(r.date),
          ...series.map((s) => (typeof r[s.key] === "number" ? format(r[s.key] as number) : "—")),
        ])}
      />
    </div>
  );
}

/** Merged PRs by size bucket: the count is the bar, the median cycle is the text. */
export function SizeBars({ rows, caption }: {
  rows: Array<{ bucket: string; max_lines: number | null; count: number; cycle_p50: number | null }>;
  caption: (count: number, cycle: string) => string;
}) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <ul className="space-y-2.5">
      {rows.map((r) => {
        const name = `${r.bucket.toUpperCase()} ${r.max_lines === null ? ">1000" : `≤${r.max_lines}`}`;
        return (
          <li key={r.bucket} className="grid grid-cols-[6.5rem_1fr] items-center gap-3 text-sm sm:grid-cols-[6.5rem_1fr_11rem]">
            <span className="tabular-nums text-[var(--color-muted-foreground)]">{name}</span>
            <span className="h-2 rounded-full bg-[var(--color-muted)]" title={caption(r.count, duration(r.cycle_p50))}>
              <span className="block h-full rounded-full bg-[var(--color-primary)] transition-[width] duration-500 ease-out-quint"
                style={{ width: `${(r.count / max) * 100}%`, opacity: r.count ? 0.85 : 0 }} />
            </span>
            <span className="col-span-2 text-xs tabular-nums text-[var(--color-muted-foreground)] sm:col-span-1 sm:text-right">
              {caption(r.count, duration(r.cycle_p50))}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** Deployments along the window; a failed one is red and taller, one still
 *  inside its failure window (not yet judged) is hollow. */
export function DeployTimeline({ rows, from, to, label, legend, tableHead }: {
  rows: Array<{ id: string; repo: string; at: string | null; failed: boolean; settled: boolean }>;
  from: string;
  to: string;
  label: string;
  legend: Series[];
  tableHead: [string, string];
}) {
  const t0 = new Date(from).getTime();
  const span = Math.max(1, new Date(to).getTime() - t0);
  const x = (iso: string | null) => (iso ? Math.min(W - 3, Math.max(3, ((new Date(iso).getTime() - t0) / span) * W)) : 3);
  const T = 56;
  return (
    <div>
      <Legend series={legend} />
      <svg viewBox={`0 0 ${W} ${T}`} className="h-14 w-full" role="img" aria-label={label} preserveAspectRatio="none">
        <line x1={0} x2={W} y1={T - 2} y2={T - 2} stroke="var(--color-border-strong)" strokeWidth={1}
          vectorEffect="non-scaling-stroke" />
        {rows.map((r) => {
          const color = r.failed ? "var(--color-destructive)" : "var(--color-primary)";
          return (
            <line key={r.id} x1={x(r.at)} x2={x(r.at)} y1={T - 2} y2={r.failed ? 6 : 24}
              stroke={color} strokeWidth={r.settled ? 3 : 2} strokeOpacity={r.settled ? 0.9 : 0.5}
              strokeDasharray={r.settled ? undefined : "3 2"} vectorEffect="non-scaling-stroke">
              <title>{`${formatDateTime(r.at)} · ${r.repo}`}</title>
            </line>
          );
        })}
      </svg>
      <Ends first={from} last={to} max={String(rows.length)} />
      <TableView
        head={tableHead}
        rows={[...rows].reverse().slice(0, 100).map((r) => [
          formatDateTime(r.at), `${r.repo}${r.failed ? " ✕" : ""}`,
        ])}
      />
    </div>
  );
}
