"use client";

// Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
// see LICENSING.md and ee/README.md in the repository root.

/**
 * The data side of /productivity: which repositories are read, how far the
 * reading has got, and the settings that decide what counts as a deployment,
 * a revert or a bot.
 *
 * Everyone who can open the page sees the progress and may ask for a sync
 * (editor and above — the API's rule). Switching a repository on and changing
 * the settings are owner/admin, so those controls are drawn disabled for the
 * rest; the API refuses them either way.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCwIcon, SettingsIcon } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { formatDateTime } from "@/lib/format";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import { useCanManageWorkspace } from "@/lib/use-workspace-role";
import {
  productivityApi, type SettingValue, type SettingsLayers, type SyncOverview,
} from "@/ee/analytics/productivity-api";

export const SYNC_KEY = ["productivity", "sync"] as const;

export function useSyncOverview(enabled: boolean) {
  const token = useToken();
  return useQuery({
    queryKey: SYNC_KEY,
    queryFn: () => productivityApi.sync(token!),
    enabled: !!token && enabled,
    // While history is loading the numbers move; otherwise nothing does.
    refetchInterval: (q) => (q.state.data?.backfilling || q.state.data?.repos.some((r) => r.status?.running) ? 15_000 : false),
  });
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ─── the panel ───────────────────────────────────────────────────────

export function SyncPanel({ sync }: { sync: SyncOverview | undefined }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const canManage = useCanManageWorkspace() === true;
  const { confirm, dialog } = useConfirm();
  const [settingsOpen, setSettingsOpen] = useState(false);

  const run = useMutation({
    mutationFn: (v: { provider: string; repo: string; full: boolean }) =>
      productivityApi.runSync(token!, v.provider, v.repo, v.full),
    onSuccess: (r) => {
      toast[r.queued ? "success" : "info"](r.queued ? t("productivity.sync.queued") : t("productivity.sync.alreadyQueued"));
      qc.invalidateQueries({ queryKey: SYNC_KEY });
    },
    onError: (e) => toast.error(messageOf(e)),
  });
  const toggle = useMutation({
    mutationFn: (v: { provider: string; repo: string; enabled: boolean }) =>
      productivityApi.saveSettings(token!, { enabled: v.enabled }, { provider: v.provider, repo: v.repo }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["productivity"] }),
    onError: (e) => toast.error(messageOf(e)),
  });

  if (!sync) return <Skeleton className="h-28 rounded-xl" />;

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle>{t("productivity.sync.title")}</CardTitle>
          <CardDescription>{t("productivity.sync.desc")}</CardDescription>
        </div>
        <Button size="sm" variant="outline" disabled={!canManage} onClick={() => setSettingsOpen(true)}>
          <SettingsIcon />
          {t("productivity.sync.settings")}
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
        {sync.repos.length === 0 && (
          <EmptyState title={t("productivity.sync.noReposTitle")} description={t("productivity.sync.noReposDesc")} />
        )}
        <ul className="divide-y divide-[var(--color-border)]">
          {sync.repos.map((r) => {
            const st = r.status;
            const limited = !!st?.rate_limited_until && new Date(st.rate_limited_until) > new Date();
            const pct = st && st.prs_total > 0 ? Math.min(100, (st.prs_detailed / st.prs_total) * 100) : 0;
            return (
              <li key={`${r.provider}:${r.repo}`} className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
                <Switch
                  checked={r.enabled}
                  disabled={!canManage || toggle.isPending}
                  aria-label={t("productivity.sync.enable", { repo: r.repo })}
                  onCheckedChange={(enabled) => toggle.mutate({ provider: r.provider, repo: r.repo, enabled })}
                />
                <div className="min-w-0 flex-1 basis-48">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate font-medium">{r.repo}</span>
                    {!r.enabled && <Badge variant="outline">{t("productivity.sync.off")}</Badge>}
                    {r.enabled && st?.running && <Badge variant="info">{t("productivity.sync.running")}</Badge>}
                    {r.enabled && limited && <Badge variant="warning">{t("productivity.sync.limited")}</Badge>}
                    {r.enabled && st?.last_error && !limited && <Badge variant="destructive">{t("productivity.sync.error")}</Badge>}
                  </div>
                  {r.enabled && (
                    <div className="mt-1 text-xs text-[var(--color-muted-foreground)]">
                      {st && st.prs_total > 0 && (
                        <span className="tabular-nums">
                          {t("productivity.sync.progress", { done: st.prs_detailed, total: st.prs_total })}
                          {" · "}
                        </span>
                      )}
                      {st?.last_ok_at
                        ? t("productivity.sync.lastOk", { when: formatDateTime(st.last_ok_at) })
                        : t("productivity.sync.never")}
                      {st?.last_error && !limited && (
                        <span className="block truncate" title={st.last_error}>
                          {t("productivity.sync.lastError", { error: st.last_error })}
                        </span>
                      )}
                      {limited && st?.rate_limited_until && (
                        <span className="block">{t("productivity.sync.limitedUntil", { when: formatDateTime(st.rate_limited_until) })}</span>
                      )}
                    </div>
                  )}
                  {r.enabled && st && !st.backfill_done && st.prs_total > 0 && (
                    <div className="mt-2 h-1.5 max-w-xs overflow-hidden rounded-full bg-[var(--color-muted)]"
                      role="progressbar" aria-valuemin={0} aria-valuemax={st.prs_total} aria-valuenow={st.prs_detailed}>
                      <div className="h-full rounded-full bg-[var(--color-primary)] transition-[width] duration-500" style={{ width: `${pct}%` }} />
                    </div>
                  )}
                </div>
                {r.enabled && (
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" loading={run.isPending && run.variables?.repo === r.repo && !run.variables?.full}
                      onClick={() => run.mutate({ provider: r.provider, repo: r.repo, full: false })}>
                      <RefreshCwIcon />
                      {t("productivity.sync.now")}
                    </Button>
                    {canManage && (
                      <Button size="sm" variant="ghost"
                        onClick={async () => {
                          if (await confirm({
                            title: t("productivity.sync.rereadTitle"), description: t("productivity.sync.rereadDesc"),
                            confirmLabel: t("productivity.sync.reread"),
                          })) run.mutate({ provider: r.provider, repo: r.repo, full: true });
                        }}>
                        {t("productivity.sync.reread")}
                      </Button>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </CardContent>
      {dialog}
      {settingsOpen && <SettingsDialog sync={sync} onClose={() => setSettingsOpen(false)} />}
    </Card>
  );
}

// ─── the settings dialog ─────────────────────────────────────────────

type FieldKind = "int" | "list" | "lines" | "text" | "bool" | "source";
const FIELDS: Array<{ name: string; kind: FieldKind; label: string; help?: string }> = [
  { name: "enabled", kind: "bool", label: "productivity.settings.enabled" },
  { name: "backfill_days", kind: "int", label: "productivity.settings.backfillDays" },
  { name: "production_branches", kind: "list", label: "productivity.settings.productionBranches", help: "productivity.settings.productionHelp" },
  { name: "integration_branches", kind: "list", label: "productivity.settings.integrationBranches", help: "productivity.settings.integrationHelp" },
  { name: "deploy_source", kind: "source", label: "productivity.settings.deploySource" },
  { name: "tag_pattern", kind: "text", label: "productivity.settings.tagPattern" },
  { name: "failure_window_days", kind: "int", label: "productivity.settings.failureWindow", help: "productivity.settings.failureHelp" },
  { name: "deploy_group_minutes", kind: "int", label: "productivity.settings.groupMinutes", help: "productivity.settings.groupHelp" },
  { name: "ignored_authors", kind: "list", label: "productivity.settings.ignoredAuthors", help: "productivity.settings.ignoredHelp" },
  { name: "revert_patterns", kind: "lines", label: "productivity.settings.revertPatterns" },
  { name: "hotfix_branch_patterns", kind: "lines", label: "productivity.settings.hotfixPatterns" },
  { name: "bugfix_patterns", kind: "lines", label: "productivity.settings.bugfixPatterns", help: "productivity.settings.patternsHelp" },
  { name: "rate_per_hour", kind: "int", label: "productivity.settings.ratePerHour", help: "productivity.settings.rateHelp" },
];

/** A stored value as the text a field shows. */
function show(v: SettingValue | undefined, kind: FieldKind): string {
  if (v === undefined || v === null) return "";
  if (Array.isArray(v)) return v.join(kind === "lines" ? "\n" : ", ");
  return String(v);
}

/** The text of a field as the value the API takes; "" = inherit. */
function parse(text: string, kind: FieldKind): SettingValue | null {
  const s = text.trim();
  if (!s) return null;
  if (kind === "bool") return s === "true";
  if (kind === "int") return Number(s);
  if (kind === "list") return s.split(",").map((x) => x.trim()).filter(Boolean);
  if (kind === "lines") return s.split("\n").map((x) => x.trim()).filter(Boolean);
  return s;
}

function SettingsDialog({ sync, onClose }: { sync: SyncOverview; onClose: () => void }) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [scope, setScope] = useState("");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [estimate, setEstimate] = useState<string | null>(null);
  const target = scope ? { provider: scope.split(":")[0], repo: scope.slice(scope.indexOf(":") + 1) } : undefined;

  const layers = useQuery({
    queryKey: ["productivity", "settings", scope],
    queryFn: () => productivityApi.settings(token!, target),
    enabled: !!token,
  });
  const data: SettingsLayers | undefined = layers.data;
  const own = (scope ? data?.repo : data?.workspace) ?? {};

  // Reset the form whenever a different layer finishes loading: state adjusted
  // during render rather than in an effect, so the form never shows stale values.
  const [loaded, setLoaded] = useState<SettingsLayers | undefined>();
  if (data && data !== loaded) {
    const next: Record<string, string> = {};
    for (const f of FIELDS) next[f.name] = show(own[f.name], f.kind);
    setLoaded(data);
    setDraft(next);
    setEstimate(null);
  }

  const save = useMutation({
    mutationFn: () => {
      const changes: Record<string, SettingValue | null> = {};
      for (const f of FIELDS) {
        const before = show(own[f.name], f.kind);
        if ((draft[f.name] ?? "") !== before) changes[f.name] = parse(draft[f.name] ?? "", f.kind);
      }
      return productivityApi.saveSettings(token!, changes, target);
    },
    onSuccess: () => {
      toast.success(t("productivity.settings.saved"));
      qc.invalidateQueries({ queryKey: ["productivity"] });
      onClose();
    },
    onError: (e) => toast.error(messageOf(e)),
  });
  const ask = useMutation({
    mutationFn: () => productivityApi.estimate(
      token!, target!.provider, target!.repo, Number(draft.backfill_days) || undefined),
    onSuccess: (r) => setEstimate(
      r.requests === null
        ? t("productivity.settings.estimateUnknown")
        : t("productivity.settings.estimate", { prs: r.prs ?? 0, requests: r.requests, hours: r.hours ?? 0 })),
    onError: (e) => setEstimate(messageOf(e)),
  });

  const inherited = (name: string, kind: FieldKind) => show((data?.resolved ?? {})[name], kind);
  // Which layer an unset field falls back to: the workspace default when it
  // sets the value, otherwise the built-in one. Only meaningful when unset.
  const source = (name: string, kind: FieldKind): string | null => {
    if (!data || (draft[name] ?? "") !== "") return null;
    if (scope && data.workspace[name] !== undefined) {
      return t("productivity.settings.fromWorkspace", { value: show(data.workspace[name], kind).replace(/\n/g, " · ") });
    }
    return t("productivity.settings.fromBuiltin", { value: show(data.builtin[name], kind).replace(/\n/g, " · ") });
  };
  const set = (name: string, value: string) => setDraft((d) => ({ ...d, [name]: value }));

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("productivity.settings.title")}</DialogTitle>
          <DialogDescription>{t("productivity.settings.desc")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5">
          <Label htmlFor="prod-scope">{t("productivity.settings.scope")}</Label>
          <Select
            id="prod-scope"
            value={scope}
            onChange={setScope}
            options={[
              { value: "", label: t("productivity.settings.scopeDefault") },
              ...sync.repos.map((r) => ({ value: `${r.provider}:${r.repo}`, label: r.repo, group: t("productivity.settings.scopeRepos") })),
            ]}
          />
        </div>

        {layers.isError && <Callout tone="danger">{t("common.loadError")}: {messageOf(layers.error)}</Callout>}
        {!data && !layers.isError && <Skeleton className="h-40 rounded-xl" />}
        {data && (
          <div className="grid gap-4 sm:grid-cols-2">
            {FIELDS.map((f) => {
              const id = `prod-${f.name}`;
              const placeholder = inherited(f.name, f.kind);
              const wide = f.kind === "lines" || f.kind === "list";
              return (
                <div key={f.name} className={`space-y-1.5 ${wide ? "sm:col-span-2" : ""}`}>
                  <Label htmlFor={id}>{t(f.label)}</Label>
                  {f.kind === "bool" && (
                    <Select id={id} value={draft[f.name] ?? ""} onChange={(v) => set(f.name, v)} options={[
                      { value: "", label: t("productivity.settings.inherit") },
                      { value: "true", label: t("productivity.settings.on") },
                      { value: "false", label: t("productivity.settings.off") },
                    ]} />
                  )}
                  {f.kind === "source" && (
                    <Select id={id} value={draft[f.name] ?? ""} onChange={(v) => set(f.name, v)} options={[
                      { value: "", label: t("productivity.settings.inherit") },
                      ...data.limits.deploy_sources.map((s) => ({ value: s, label: t(`productivity.settings.source.${s}`) })),
                    ]} />
                  )}
                  {(f.kind === "int" || f.kind === "text" || f.kind === "list") && (
                    <Input id={id} value={draft[f.name] ?? ""} placeholder={placeholder}
                      type={f.kind === "int" ? "number" : "text"} inputMode={f.kind === "int" ? "numeric" : undefined}
                      onChange={(e) => set(f.name, e.target.value)} />
                  )}
                  {f.kind === "lines" && (
                    <Textarea id={id} rows={2} value={draft[f.name] ?? ""} placeholder={placeholder}
                      className="font-mono text-xs" onChange={(e) => set(f.name, e.target.value)} />
                  )}
                  {source(f.name, f.kind) && (
                    <p className="text-xs text-[var(--color-muted-foreground)]">{source(f.name, f.kind)}</p>
                  )}
                  {f.help && <p className="text-xs text-[var(--color-muted-foreground)]">{t(f.help)}</p>}
                </div>
              );
            })}
          </div>
        )}

        {target && (
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Button size="sm" variant="outline" loading={ask.isPending} onClick={() => ask.mutate()}>
              {t("productivity.settings.estimateButton")}
            </Button>
            {estimate && <span className="text-[var(--color-muted-foreground)]">{estimate}</span>}
          </div>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>{t("common.cancel")}</Button>
          <Button loading={save.isPending} disabled={!data} onClick={() => save.mutate()}>
            {t("common.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
