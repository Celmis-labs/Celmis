"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowLeftIcon, RotateCcwIcon, SaveIcon } from "lucide-react";

import { agentsApi } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";

export default function AgentEditPage() {
  const params = useParams<{ name: string }>();
  const name = params.name;
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  const { confirm, dialog } = useConfirm();

  const agent = useQuery({
    queryKey: ["agents", name],
    queryFn: () => agentsApi.get(token!, name),
    enabled: !!token,
  });

  const [prompt, setPrompt] = useState("");
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!agent.data) return;
    setPrompt(agent.data.system_prompt);
    setDirty(false);
  }, [agent.data]);

  const save = useMutation({
    mutationFn: () => agentsApi.overridePrompt(token!, name, prompt),
    onSuccess: () => {
      toast.success(t("admin.agents.detail.savedToast"));
      setDirty(false);
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (e) => toast.error(t("admin.agents.detail.saveFailed", { message: (e as Error).message })),
  });

  const reset = useMutation({
    mutationFn: () => agentsApi.resetPrompt(token!, name),
    onSuccess: () => {
      toast.success(t("admin.agents.detail.resetToast"));
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (e) => toast.error(t("admin.agents.detail.resetFailed", { message: (e as Error).message })),
  });

  if (agent.isError) {
    return (
      <div className="mx-auto w-full max-w-4xl p-8">
        <Callout tone="danger">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>
              {t("common.loadError")}
              {(agent.error as Error)?.message ? `: ${(agent.error as Error).message}` : ""}
            </span>
            <Button size="sm" variant="outline" onClick={() => agent.refetch()}>
              {t("common.retry")}
            </Button>
          </div>
        </Callout>
      </div>
    );
  }
  if (!agent.data) {
    return (
      <div className="mx-auto w-full max-w-4xl space-y-6 p-8">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-72 w-full" />
      </div>
    );
  }
  const a = agent.data;

  return (
    <PageShell width="wide">
      <div>
        <Link
          href="/admin/agents"
          className="text-sm text-[var(--color-muted-foreground)] inline-flex items-center gap-1 hover:underline"
        >
          <ArrowLeftIcon className="h-3.5 w-3.5" /> {t("admin.agents.detail.backToAgents")}
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight mt-2 flex items-center gap-2">
          {a.display_name}
          {a.has_override && (
            <Badge variant="outline" className="text-[10px]">{t("admin.agents.detail.customPromptActive")}</Badge>
          )}
        </h1>
        <p className="text-sm text-[var(--color-muted-foreground)] mt-1">
          {a.role}
        </p>
      </div>

      <SectionTabs set="review" />

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.agents.detail.roleAndImpact")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div>
            <strong>{t("admin.agents.detail.verdictImpact")}</strong>{" "}
            <span className={a.verdict_impact.startsWith("critical") ? "text-red-600" : ""}>
              {a.verdict_impact}
            </span>
          </div>
          <div>
            <strong>{t("admin.agents.detail.defaultSeverity")}</strong> <code>{a.default_severity}</code>
          </div>
          <div>
            <strong>{t("admin.agents.detail.whatItChecks")}</strong>
            <ul className="mt-1 space-y-1 list-disc list-inside">
              {a.focus.map((f, i) => (
                <li key={i}>{f}</li>
              ))}
            </ul>
          </div>
          <div>
            <strong>{t("admin.agents.detail.contextProvided")}</strong>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {a.context_used.map((c, i) => (
                <Badge key={i} variant="outline" className="text-[10px]">{c}</Badge>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.agents.detail.systemPrompt")}</CardTitle>
          <CardDescription>
            {t("admin.agents.detail.systemPromptDesc")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Textarea
            rows={22}
            value={prompt}
            onChange={(e) => {
              setPrompt(e.target.value);
              setDirty(true);
            }}
            className="font-mono text-xs"
          />
          <p className="text-xs text-[var(--color-muted-foreground)] mt-2">
            {t("admin.agents.detail.charCount", { count: prompt.length, original: a.system_prompt.length })}
            {a.has_override && t("admin.agents.detail.currentlyOverridden")}
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.agents.detail.userPromptTemplate")}</CardTitle>
          <CardDescription>
            {t("admin.agents.detail.userPromptDescPre")}<code>{"{diff}"}</code>, <code>{"{graph_summary}"}</code>,
            <code>{" {cross_repo_drift}"}</code>{t("admin.agents.detail.userPromptDescPost")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Textarea
            rows={12}
            value={a.user_prompt_template}
            readOnly
            className="font-mono text-base sm:text-xs opacity-70"
          />
        </CardContent>
      </Card>

      <div className="flex items-center justify-between sticky bottom-0 bg-[var(--color-background)] border-t border-[var(--color-border)] py-3">
        <Button
          variant="ghost"
          onClick={async () => {
            const ok = await confirm({
              title: t("admin.agents.detail.resetConfirm"),
              danger: true,
            });
            if (ok) reset.mutate();
          }}
          disabled={reset.isPending || !a.has_override}
        >
          <RotateCcwIcon className="h-4 w-4 mr-1" /> {t("admin.agents.detail.resetToDefault")}
        </Button>
        <div className="flex items-center gap-2">
          {dirty && (
            <span className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.agents.detail.unsavedChanges")}
            </span>
          )}
          <Button
            onClick={() => save.mutate()}
            disabled={save.isPending || !dirty}
          >
            <SaveIcon className="h-4 w-4 mr-1" />
            {save.isPending ? t("admin.agents.detail.saving") : t("admin.agents.detail.saveOverride")}
          </Button>
        </div>
      </div>
      {dialog}
    </PageShell>
  );
}
