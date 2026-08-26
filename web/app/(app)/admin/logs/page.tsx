"use client";

/**
 * /admin/logs — live server log tail + one-shot state dump.
 *
 * The box is not always SSH-reachable, so this is how an operator (or the
 * person helping them) sees what the API is actually doing: the in-memory
 * ring buffer from src/ops/logbuf.py, plus a queue/audit/session snapshot.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { CopyIcon, ScrollTextIcon, StethoscopeIcon } from "lucide-react";

import { opsApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { AdminGate } from "@/components/admin-gate";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { QueryState } from "@/components/ui/query-state";
import { Select } from "@/components/ui/select";

const LEVEL_TONE: Record<string, string> = {
  ERROR: "text-red-500",
  CRITICAL: "text-red-500 font-semibold",
  WARNING: "text-amber-500",
  INFO: "text-[var(--color-muted-foreground)]",
  DEBUG: "text-[var(--color-muted-foreground)] opacity-70",
};

export default function LogsPage() {
  const t = useT();
  const token = useToken();
  const [level, setLevel] = useState("");
  const [contains, setContains] = useState("");
  const [live, setLive] = useState(true);

  const logs = useQuery({
    queryKey: ["ops", "logs", level, contains],
    queryFn: () => opsApi.logs(token!, { limit: 500, level: level || undefined,
                                          contains: contains || undefined }),
    enabled: !!token,
    refetchInterval: live ? 5000 : false,
  });

  const diag = useQuery({
    queryKey: ["ops", "diag"],
    queryFn: () => opsApi.diag(token!),
    enabled: !!token,
  });

  const copyAll = () => {
    const text = (logs.data?.records ?? [])
      .map((r) => `${r.ts} ${r.level} ${r.logger} (${r.module}) ${r.message}`
        + (r.exc ? `\n${r.exc}` : ""))
      .join("\n");
    void navigator.clipboard.writeText(text);
    toast.success(t("admin.logs.copied"));
  };

  return (
    <AdminGate>
      <PageShell width="wide">
        <PageHeader
          icon={<ScrollTextIcon className="h-6 w-6" />}
          title={t("admin.logs.title")}
          description={t("admin.logs.subtitle")}
          tabs={<SectionTabs set="monitoring" />}
        />

        <Card>
          <CardHeader className="pb-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <CardTitle className="text-base">{t("admin.logs.tailTitle")}</CardTitle>
                <CardDescription>
                  {logs.data
                    ? t("admin.logs.buffered", {
                        n: logs.data.stats.buffered,
                        cap: logs.data.stats.capacity,
                      })
                    : ""}
                </CardDescription>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Select
                  className="h-11 sm:h-8 min-w-32 text-base sm:text-xs"
                  value={level}
                  onChange={setLevel}
                  options={[
                    { value: "", label: t("admin.logs.levelAll") },
                    { value: "INFO", label: "INFO+" },
                    { value: "WARNING", label: "WARNING+" },
                    { value: "ERROR", label: "ERROR+" },
                  ]}
                />
                <Input
                  className="h-11 sm:h-8 w-44 text-base sm:text-xs"
                  placeholder={t("admin.logs.filterPlaceholder")}
                  value={contains}
                  onChange={(e) => setContains(e.target.value)}
                />
                <Button variant="outline" size="sm" onClick={() => setLive((v) => !v)}>
                  {live ? t("admin.logs.pause") : t("admin.logs.resume")}
                </Button>
                <Button variant="ghost" size="sm" onClick={copyAll}>
                  <CopyIcon className="mr-1 h-3.5 w-3.5" /> {t("admin.logs.copy")}
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <QueryState
              query={logs}
              empty={{ icon: ScrollTextIcon, title: t("admin.logs.empty") }}
            >
              {(data) => (
                <div className="max-h-[60vh] overflow-auto rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/20 p-2 font-mono text-[11px] leading-relaxed">
                  {data.records.length === 0 && (
                    <div className="p-2 text-[var(--color-muted-foreground)]">
                      {t("admin.logs.empty")}
                    </div>
                  )}
                  {data.records.map((r, i) => (
                    <div key={i} className="border-b border-[var(--color-border)]/40 py-0.5 last:border-0">
                      <span className="text-[var(--color-muted-foreground)]">
                        {r.ts.slice(11, 19)}
                      </span>{" "}
                      <span className={LEVEL_TONE[r.level] ?? ""}>{r.level}</span>{" "}
                      <span className="text-[var(--color-muted-foreground)]">
                        {r.logger}
                      </span>{" "}
                      <span className="break-all">{r.message}</span>
                      {r.exc && (
                        <pre className="mt-1 whitespace-pre-wrap text-red-500/80">
                          {r.exc}
                        </pre>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </QueryState>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-base">
              <StethoscopeIcon className="h-4 w-4" /> {t("admin.logs.diagTitle")}
            </CardTitle>
            <CardDescription>{t("admin.logs.diagSubtitle")}</CardDescription>
          </CardHeader>
          <CardContent>
            <QueryState query={diag}>
              {(data) => (
                <div className="space-y-2">
                  <div className="flex flex-wrap gap-1">
                    {Object.entries(
                      (data.jobs_by_status ?? {}) as Record<string, number>,
                    ).map(([k, v]) => (
                      <Badge key={k} variant="outline" className="font-mono text-[10px]">
                        {k}: {v}
                      </Badge>
                    ))}
                  </div>
                  <pre className="max-h-80 overflow-auto rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/20 p-2 font-mono text-[11px]">
                    {JSON.stringify(data, null, 2)}
                  </pre>
                  <Button
                    variant="ghost" size="sm"
                    onClick={() => {
                      void navigator.clipboard.writeText(JSON.stringify(data, null, 2));
                      toast.success(t("admin.logs.copied"));
                    }}
                  >
                    <CopyIcon className="mr-1 h-3.5 w-3.5" /> {t("admin.logs.copy")}
                  </Button>
                </div>
              )}
            </QueryState>
          </CardContent>
        </Card>
      </PageShell>
    </AdminGate>
  );
}
