"use client";

/**
 * /admin/deprecations — track deprecated symbols + scan consumers.
 * Each row is one deprecation; the scan button refreshes `consumers`
 * across every indexed repo. MCP tool `list_deprecations` exposes the
 * same data to Claude Code so it can avoid picking soon-to-be-removed
 * symbols when writing new integrations.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PlusIcon, RadarIcon, Trash2Icon, TimerIcon } from "lucide-react";

import { intelApi, type Deprecation } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { HelpButton } from "@/components/ui/help-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function DeprecationsPage() {
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  const [helpOpen, setHelpOpen] = useState(false);
  const list = useQuery({
    queryKey: ["deprecations"],
    queryFn: () => intelApi.listDeprecations(token!),
    enabled: !!token,
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<TimerIcon className="h-6 w-6" />}
        title={t("admin.deprecations.title")}
        description={
          <>
            {t("admin.deprecations.descriptionBefore")}
            <code className="mx-1">list_deprecations</code>
            {t("admin.deprecations.descriptionAfter")}
          </>
        }
        actions={
          <HelpButton onClick={() => setHelpOpen(true)} aria-label={t("admin.deprecations.helpTitle")} />
        }
        tabs={<SectionTabs set="review" />}
      />

      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("admin.deprecations.helpTitle")}</DialogTitle>
            <DialogDescription>{t("admin.deprecations.helpIntro")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <div>
              <div className="mb-1 font-medium">{t("admin.deprecations.helpUsesTitle")}</div>
              <ul className="list-disc space-y-1 pl-5 text-xs text-[var(--color-muted-foreground)]">
                <li>{t("admin.deprecations.helpUse1")}</li>
                <li>{t("admin.deprecations.helpUse2")}</li>
                <li>{t("admin.deprecations.helpUse3")}</li>
              </ul>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("admin.deprecations.helpFieldsTitle")}</div>
              <div className="space-y-2 text-xs">
                {([
                  ["repo_slug", "helpFieldRepo"],
                  ["symbol", "helpFieldSymbol"],
                  ["replacement", "helpFieldReplacement"],
                  ["target_removal", "helpFieldRemoval"],
                  ["reason", "helpFieldReason"],
                ] as const).map(([field, key]) => (
                  <div key={field}>
                    <code className="text-[11px]">{field}</code>
                    <span className="block text-[var(--color-muted-foreground)]">
                      {t(`admin.deprecations.${key}`)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.deprecations.helpTip")}
            </p>
          </div>
        </DialogContent>
      </Dialog>

      <NewDeprecation onCreated={() => qc.invalidateQueries({ queryKey: ["deprecations"] })} />

      {(list.data ?? []).length === 0 && (
        <p className="text-sm text-[var(--color-muted-foreground)]">{t("admin.deprecations.emptyState")}</p>
      )}
      {(list.data ?? []).map((d) => (
        <DepRow key={d.id} d={d} onChanged={() => qc.invalidateQueries({ queryKey: ["deprecations"] })} />
      ))}
    </PageShell>
  );
}

function NewDeprecation({ onCreated }: { onCreated: () => void }) {
  const token = useToken();
  const t = useT();
  const [repo, setRepo] = useState("");
  const [sym, setSym] = useState("");
  const [reason, setReason] = useState("");
  const [repl, setRepl] = useState("");
  const [target, setTarget] = useState("");

  const create = useMutation({
    mutationFn: () => intelApi.createDeprecation(token!, {
      repo_slug: repo.trim(),
      symbol: sym.trim(),
      reason,
      replacement: repl || null,
      target_removal_at: target || null,
    }),
    onSuccess: () => {
      setRepo(""); setSym(""); setReason(""); setRepl(""); setTarget("");
      onCreated(); toast.success(t("admin.deprecations.toastAdded"));
    },
    onError: (e: Error) => toast.error(t("admin.deprecations.createFailed", { message: e.message })),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <PlusIcon className="h-4 w-4" /> {t("admin.deprecations.newTitle")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="grid gap-2 sm:grid-cols-2">
          <div>
            <Label>{t("admin.deprecations.repoSlugLabel")}</Label>
            <Input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="owner/repo" />
          </div>
          <div>
            <Label>{t("admin.deprecations.symbolLabel")}</Label>
            <Input value={sym} onChange={(e) => setSym(e.target.value)}
                   placeholder={t("admin.deprecations.symbolPlaceholder")} />
          </div>
          <div>
            <Label>{t("admin.deprecations.replacementLabel")}</Label>
            <Input value={repl} onChange={(e) => setRepl(e.target.value)} placeholder="module.new_function" />
          </div>
          <div>
            <Label>{t("admin.deprecations.targetRemovalLabel")}</Label>
            <Input value={target} onChange={(e) => setTarget(e.target.value)} placeholder="2027-01-01" />
          </div>
        </div>
        <div>
          <Label>{t("admin.deprecations.reasonLabel")}</Label>
          <Input value={reason} onChange={(e) => setReason(e.target.value)}
                 placeholder={t("admin.deprecations.reasonPlaceholder")} />
        </div>
        <div className="flex justify-end">
          <Button disabled={!repo.trim() || !sym.trim() || create.isPending}
                  onClick={() => create.mutate()}>
            {create.isPending ? "…" : t("admin.deprecations.addButton")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function DepRow({ d, onChanged }: { d: Deprecation; onChanged: () => void }) {
  const token = useToken();
  const t = useT();
  const { confirm, dialog } = useConfirm();
  const del = useMutation({
    mutationFn: () => intelApi.deleteDeprecation(token!, d.id),
    onSuccess: () => { onChanged(); toast.success(t("admin.deprecations.toastRemoved")); },
  });
  const scan = useMutation({
    mutationFn: () => intelApi.scanDeprecation(token!, d.id),
    onSuccess: (r) => {
      onChanged();
      toast.success(t("admin.deprecations.scanComplete", { count: r.consumers.length }));
    },
    onError: (e: Error) => toast.error(t("admin.deprecations.scanFailed", { message: e.message })),
  });
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <CardTitle className="text-base flex items-center gap-2 flex-wrap">
              <code>{d.symbol}</code>
              <Badge variant="outline">{d.repo_slug}</Badge>
              {d.target_removal_at && (
                <Badge variant="outline" className="font-mono text-[10px]">
                  {t("admin.deprecations.removesBadge", { date: d.target_removal_at.slice(0, 10) })}
                </Badge>
              )}
              <Badge variant={d.consumers.length ? "brand" : "outline"}>
                {t("admin.deprecations.consumersBadge", { count: d.consumers.length })}
              </Badge>
            </CardTitle>
            {d.reason && <CardDescription className="mt-1">{d.reason}</CardDescription>}
            {d.replacement && (
              <div className="text-xs text-[var(--color-muted-foreground)] mt-1">
                {t("admin.deprecations.replacementPrefix")} <code>{d.replacement}</code>
              </div>
            )}
          </div>
          <div className="flex gap-1 shrink-0">
            <Button variant="ghost" size="sm" onClick={() => scan.mutate()} disabled={scan.isPending}>
              <RadarIcon className="h-3.5 w-3.5 mr-1" />
              {scan.isPending ? "…" : t("admin.deprecations.scanButton")}
            </Button>
            <Button variant="ghost" size="icon" onClick={async () => {
              const ok = await confirm({
                title: t("admin.deprecations.confirmDelete", { symbol: d.symbol }),
                confirmLabel: t("common.delete"),
                danger: true,
              });
              if (ok) del.mutate();
            }}>
              <Trash2Icon className="h-4 w-4 text-red-600" />
            </Button>
          </div>
        </div>
      </CardHeader>
      {d.consumers.length > 0 && (
        <CardContent>
          <details>
            <summary className="cursor-pointer text-xs text-[var(--color-muted-foreground)]">
              {t("admin.deprecations.consumersSummary", { count: d.consumers.length })}
            </summary>
            <ul className="mt-2 text-xs space-y-1 font-mono">
              {d.consumers.slice(0, 30).map((c, i) => (
                <li key={i}>
                  <code>{c.repo_slug}</code>
                  {c.file && <> · {c.file}{c.line ? `:${c.line}` : ""}</>}
                  {c.symbol && <> — {c.symbol}</>}
                </li>
              ))}
              {d.consumers.length > 30 && <li>{t("admin.deprecations.andMore", { count: d.consumers.length - 30 })}</li>}
            </ul>
          </details>
        </CardContent>
      )}
      {dialog}
    </Card>
  );
}
