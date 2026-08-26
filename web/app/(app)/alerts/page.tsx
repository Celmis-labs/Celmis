"use client";

/**
 * /alerts — monitoring alerts ingested from Grafana / any webhook.
 * Each alert has "Fix with Claude": jumps to /claude with the alert context
 * pre-filled so a bug can be dispatched to the agent from a phone.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { BellRingIcon, BotIcon, CheckIcon, CopyIcon, RefreshCwIcon } from "lucide-react";

import { alertsApi, type IncomingAlert } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { AlertSetupGuide } from "@/components/alert-setup-guide";
import { HelpButton } from "@/components/ui/help-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { QueryState } from "@/components/ui/query-state";
import { WorkspaceBadge } from "@/components/workspace-badge";

const SEV_VARIANT: Record<string, "default" | "brand" | "destructive"> = {
  info: "default", warning: "brand", error: "destructive", critical: "destructive",
};

export default function AlertsPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const router = useRouter();

  const alerts = useQuery({
    queryKey: ["alerts"],
    queryFn: () => alertsApi.list(token!),
    enabled: !!token,
    refetchInterval: 30_000,
  });

  const patch = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      alertsApi.patch(token!, id, { status }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
    onError: (e) => toast.error((e as Error).message),
  });

  const fixWithClaude = (a: IncomingAlert) => {
    const prompt =
      `Fix this production alert:\n\n${a.title}\n\n${a.body}`.slice(0, 4000);
    const q = new URLSearchParams({ prompt });
    if (a.repo_hint) q.set("repo", a.repo_hint);
    q.set("alert", a.id);
    router.push(`/claude?${q.toString()}`);
  };

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<BellRingIcon className="h-6 w-6" />}
        title={t("alerts.title")}
        badge={<WorkspaceBadge />}
        description={t("alerts.subtitle")}
        tabs={<SectionTabs set="monitoring" />}
      />

      <IngestCard />

      <AlertSetupGuide />

      <Card>
        <CardHeader>
          <CardTitle>{t("alerts.listTitle")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <QueryState
            query={alerts}
            empty={{ icon: BellRingIcon, title: t("alerts.empty") }}
          >
            {(data) => data.map((a) => (
            <div key={a.id} className="rounded-md border border-[var(--color-border)] p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <Badge variant={SEV_VARIANT[a.severity] ?? "default"}>{a.severity}</Badge>
                  <span className="truncate text-sm font-medium">{a.title}</span>
                </div>
                <span className="text-xs text-[var(--color-muted-foreground)]">
                  {a.source} · {formatDateTime(a.created_at)}
                </span>
              </div>
              {a.body && (
                <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap rounded bg-[var(--color-muted)]/30 px-2 py-1.5 text-xs text-[var(--color-muted-foreground)]">
                  {a.body}
                </pre>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {a.status !== "fixed" && (
                  <Button size="sm" onClick={() => fixWithClaude(a)}>
                    <BotIcon className="mr-1 h-3.5 w-3.5" /> {t("alerts.fixWithClaude")}
                  </Button>
                )}
                {a.status === "new" && (
                  <Button size="sm" variant="outline"
                    onClick={() => patch.mutate({ id: a.id, status: "acked" })}>
                    {t("alerts.ack")}
                  </Button>
                )}
                {a.status !== "fixed" ? (
                  <Button size="sm" variant="ghost"
                    onClick={() => patch.mutate({ id: a.id, status: "fixed" })}>
                    <CheckIcon className="mr-1 h-3.5 w-3.5" /> {t("alerts.markFixed")}
                  </Button>
                ) : (
                  <Badge variant="success">{t("alerts.fixed")}</Badge>
                )}
                {a.status === "acked" && <Badge>{t("alerts.acked")}</Badge>}
              </div>
            </div>
            ))}
          </QueryState>
        </CardContent>
      </Card>
    </PageShell>
  );
}

function IngestCard() {
  const t = useT();
  const token = useToken();
  const [revealed, setRevealed] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);

  const ingest = useQuery({
    queryKey: ["alerts-ingest-token"],
    queryFn: () => alertsApi.ingestToken(token!),
    enabled: !!token && revealed,
    retry: false,
  });
  const create = useMutation({
    mutationFn: () => alertsApi.createIngestToken(token!),
    onSuccess: () => ingest.refetch(),
    onError: (e) => toast.error((e as Error).message),
  });

  const url = ingest.data?.ingest_path
    ? `${window.location.origin}/backend${ingest.data.ingest_path}`
    : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          {t("alerts.ingestTitle")}
          <HelpButton onClick={() => setHelpOpen(true)} aria-label={t("alerts.helpTitle")} />
        </CardTitle>
        <CardDescription>{t("alerts.ingestDesc")}</CardDescription>
      </CardHeader>

      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("alerts.helpTitle")}</DialogTitle>
            <DialogDescription>{t("alerts.helpIntro")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 text-sm">
            <div>
              <div className="mb-1 font-medium">{t("alerts.helpStep1")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("alerts.helpStep1Body")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("alerts.helpGrafanaTitle")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("alerts.helpGrafanaBody")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("alerts.helpGenericTitle")}</div>
              <p className="mb-1 text-xs text-[var(--color-muted-foreground)]">{t("alerts.helpGenericBody")}</p>
              <pre className="overflow-x-auto rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/40 px-3 py-2 text-[11px] leading-relaxed">
{`curl -X POST <ingest-url> \\
  -H 'Content-Type: application/json' \\
  -d '{
    "title": "Payment API 500s",
    "body": "rate > 5% on /api/pay",
    "severity": "critical",
    "repo": "owner/payments"
  }'`}
              </pre>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("alerts.helpRepoTitle")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("alerts.helpRepoBody")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("alerts.helpThenTitle")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("alerts.helpThenBody")}</p>
            </div>
          </div>
        </DialogContent>
      </Dialog>
      <CardContent className="space-y-2">
        {!revealed ? (
          <Button size="sm" variant="outline" onClick={() => setRevealed(true)}>
            {t("alerts.showIngest")}
          </Button>
        ) : url ? (
          <div className="flex flex-wrap items-center gap-2">
            <code className="max-w-full break-all rounded bg-[var(--color-muted)]/40 px-2 py-1 text-xs">{url}</code>
            <Button size="sm" variant="ghost" onClick={() => {
              void navigator.clipboard.writeText(url);
              toast.success(t("alerts.copied"));
            }}>
              <CopyIcon className="h-3.5 w-3.5" />
            </Button>
            <Button size="sm" variant="ghost" onClick={() => create.mutate()}>
              <RefreshCwIcon className="mr-1 h-3.5 w-3.5" /> {t("alerts.rotate")}
            </Button>
          </div>
        ) : (
          <Button size="sm" disabled={create.isPending} onClick={() => create.mutate()}>
            {t("alerts.createIngest")}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
