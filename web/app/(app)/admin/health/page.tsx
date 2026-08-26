"use client";

/**
 * /admin/health — integration health dashboard (Stage 21).
 * One card per integration: git connections, LLM keys, Qdrant, MCP
 * sources, notification channels, job queue. Auto-refreshes.
 */

import { useState } from "react";
import { toast } from "sonner";
import { useQuery } from "@tanstack/react-query";
import { ActivitySquareIcon, DownloadIcon, GaugeIcon } from "lucide-react";

import { downloadWithAuth, integrationsHealthApi, opsApi, type IntegrationCard, type ResourceSampleOut } from "@/lib/api";
import { AdminGate } from "@/components/admin-gate";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { QueryState } from "@/components/ui/query-state";

const STATUS_TONE: Record<string, string> = {
  connected: "text-emerald-600",
  healthy: "text-emerald-600",
  enabled: "text-emerald-600",
  configured: "text-blue-600",
  not_configured: "text-[var(--color-muted-foreground)]",
  disabled: "text-[var(--color-muted-foreground)]",
  degraded: "text-amber-600 font-semibold",
  unreachable: "text-red-600 font-semibold",
  error: "text-red-600 font-semibold",
};

/** status slug → i18n key; unknown statuses fall back to the raw slug. */
const STATUS_KEY: Record<string, string> = {
  connected: "admin.health.status.connected",
  not_configured: "admin.health.status.not_configured",
  degraded: "admin.health.status.degraded",
  unreachable: "admin.health.status.unreachable",
  healthy: "admin.health.status.healthy",
  error: "admin.health.status.error",
  configured: "admin.health.status.configured",
  enabled: "admin.health.status.enabled",
  disabled: "admin.health.status.disabled",
};

const KIND_KEY: Record<string, string> = {
  git: "admin.health.kindGit",
  llm: "admin.health.kindLlm",
  vector: "admin.health.kindVector",
  mcp: "admin.health.kindMcp",
  notification: "admin.health.kindNotification",
  queue: "admin.health.kindQueue",
};

export default function HealthPage() {
  const token = useToken();
  const t = useT();
  const q = useQuery({
    queryKey: ["integrations-health"],
    queryFn: () => integrationsHealthApi.get(token!),
    enabled: !!token,
    refetchInterval: 30_000,
  });

  return (
    <AdminGate>
    <PageShell width="wide">
      <PageHeader
        icon={<ActivitySquareIcon className="h-6 w-6" />}
        title={t("admin.health.heading")}
        description={t("admin.health.description")}
        tabs={<SectionTabs set="admin" />}
      />

      <QueryState query={q}>
        {(data) => {
          const byKind: Record<string, IntegrationCard[]> = {};
          for (const c of data.cards ?? []) {
            (byKind[c.kind] ||= []).push(c);
          }
          return Object.entries(byKind).map(([kind, cards]) => (
            <Card key={kind}>
              <CardHeader className="pb-2">
                <CardTitle className="text-base">
                  {KIND_KEY[kind] ? t(KIND_KEY[kind]) : kind}
                </CardTitle>
              </CardHeader>
              <CardContent className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {cards.map((c, i) => (
                  <div key={i} className="border border-[var(--color-border)] rounded p-3">
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-sm">{c.name}</span>
                      <Badge variant="outline" className={STATUS_TONE[c.status] ?? ""}>
                        {STATUS_KEY[c.status] ? t(STATUS_KEY[c.status]) : c.status}
                      </Badge>
                    </div>
                    <div className="text-xs text-[var(--color-muted-foreground)] mt-1 truncate"
                         title={c.detail}>
                      {c.detail}
                    </div>
                  </div>
                ))}
              </CardContent>
            </Card>
          ));
        }}
      </QueryState>
      <ResourceHistory />
    </PageShell>
    </AdminGate>
  );
}


// ─── Resource history (sampler) ─────────────────────────────────────

function Spark({ data, color }: { data: number[]; color: string }) {
  if (data.length < 2) return <span className="text-xs opacity-50">—</span>;
  const max = Math.max(...data, 1);
  const pts = data.map((v, i) =>
    `${(i / (data.length - 1)) * 100},${30 - (v / max) * 28}`).join(" ");
  return (
    <svg viewBox="0 0 100 30" preserveAspectRatio="none" className="h-8 w-full">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5"
        vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function ResourceHistory() {
  const token = useToken();
  const [csvBusy, setCsvBusy] = useState(false);
  const t = useT();
  const [hours, setHours] = useState("24");
  const q = useQuery({
    queryKey: ["ops-metrics", hours],
    queryFn: () => opsApi.metrics(token!, Number(hours)),
    enabled: !!token,
    refetchInterval: 60_000,
  });
  const s = q.data?.samples ?? [];
  const a = q.data?.aggregates ?? {};
  const series = (f: keyof ResourceSampleOut) => s.map((r) => Number(r[f]) || 0);

  const rows: Array<{ key: string; label: string; unit: string; total?: boolean }> = [
    { key: "cpu_pct", label: t("admin.health.rhCpu"), unit: "%" },
    { key: "rss_mb", label: t("admin.health.rhRss"), unit: "MB" },
    { key: "sys_mem_pct", label: t("admin.health.rhSysMem"), unit: "%" },
    { key: "reviews_running", label: t("admin.health.rhReviews"), unit: "" },
    { key: "jobs_running", label: t("admin.health.rhJobs"), unit: "" },
    { key: "agent_sessions_running", label: t("admin.health.rhAgents"), unit: "" },
    { key: "llm_calls", label: t("admin.health.rhLlmCalls"), unit: "", total: true },
    { key: "llm_tokens_out", label: t("admin.health.rhTokensOut"), unit: "", total: true },
    { key: "http_requests", label: t("admin.health.rhHttp"), unit: "", total: true },
  ];

  return (
    <Card className="mt-6">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <GaugeIcon className="h-4 w-4" /> {t("admin.health.rhTitle")}
          </CardTitle>
          <div className="flex items-center gap-2">
            <Select className="h-11 sm:h-8 w-28 text-base sm:text-xs" value={hours} onChange={setHours}
              options={[
                { value: "1", label: "1h" }, { value: "6", label: "6h" },
                { value: "24", label: "24h" }, { value: "72", label: "3d" },
                { value: "168", label: "7d" },
              ]} />
            {/* A browser navigation carries no Authorization header, so this
                used to download a file containing
                {"detail":"Missing or invalid Authorization header"} under a
                button labelled CSV. Exactly the bug already found and fixed
                for the audit export next door — the fix was never applied
                here. `downloadWithAuth` sends the token and the workspace
                hint and saves the real file. */}
            <Button size="sm" variant="outline" disabled={csvBusy}
              onClick={async () => {
                setCsvBusy(true);
                try {
                  await downloadWithAuth(
                    opsApi.csvUrl(Number(hours)),
                    `celmis-resources-${hours}h.csv`,
                    token,
                  );
                } catch (e) {
                  toast.error((e as Error).message);
                } finally {
                  setCsvBusy(false);
                }
              }}>
              <DownloadIcon className="mr-1 h-3.5 w-3.5" /> CSV
            </Button>
          </div>
        </div>
        <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.health.rhDesc")}</p>
      </CardHeader>
      <CardContent>
        {s.length === 0 ? (
          <div className="py-4 text-sm text-[var(--color-muted-foreground)]">
            {t("admin.health.rhEmpty")}
          </div>
        ) : (
          <div className="grid gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
            {rows.map((r) => (
              <div key={r.key} className="rounded-md border border-[var(--color-border)] p-2">
                <div className="flex items-baseline justify-between text-xs">
                  <span className="text-[var(--color-muted-foreground)]">{r.label}</span>
                  <span className="tabular-nums">
                    {r.total
                      ? `Σ ${a[r.key]?.total ?? 0}`
                      : `avg ${a[r.key]?.avg ?? 0}${r.unit} · max ${a[r.key]?.max ?? 0}${r.unit}`}
                  </span>
                </div>
                <Spark data={series(r.key as keyof ResourceSampleOut)} color="var(--color-brand)" />
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
