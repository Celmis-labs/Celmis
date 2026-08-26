"use client";

/**
 * /admin/access — fine-grained *research* access (Stage 22).
 *
 * Controls what a team may LEARN about a repo through Q&A / graph / vector
 * search — down to individual paths. Enforced identically across UI, REST and
 * MCP. Distinct from /admin/teams which governs PR-review write permission.
 *
 *   visibility: none | metadata | code
 *   deny_globs: paths always hidden (creds / crypto / db) — win over allow
 *   allow_globs: if set, an allow-list (deny still subtracts)
 */

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ShieldHalfIcon, Trash2Icon, PlusIcon, EyeOffIcon } from "lucide-react";

import {
  api, accessApi, teamsApi,
  type AccessRule, type AccessVisibility, type RepoOut,
} from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { useToken } from "@/lib/use-token";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select } from "@/components/ui/select";
import { useConfirm } from "@/components/ui/confirm-dialog";

const DENY_PRESETS: Record<string, string[]> = {
  Credentials: ["**/credentials/**", "**/*secret*", "**/.env*", "**/vault/**"],
  "Crypto / algorithms": ["**/crypto/**", "**/cipher/**", "**/algorithm*/**"],
  "DB connections": ["**/db/connection*", "**/database.*", "**/*dsn*", "**/migrations/**"],
};

const VIS_BADGE: Record<AccessVisibility, string> = {
  none: "bg-red-500/15 text-red-600 dark:text-red-400",
  metadata: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  code: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
};

const PRESET_LABEL_KEYS: Record<string, string> = {
  Credentials: "admin.access.presetCredentials",
  "Crypto / algorithms": "admin.access.presetCrypto",
  "DB connections": "admin.access.presetDb",
};

function linesToArr(s: string): string[] {
  return s.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
}

export default function AccessPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();

  const rules = useQuery({
    queryKey: ["access", "rules"],
    queryFn: () => accessApi.listRules(token!),
    enabled: !!token,
  });
  const teams = useQuery({
    queryKey: ["teams"],
    queryFn: () => teamsApi.list(token!),
    enabled: !!token,
  });
  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });

  // form state
  const [repoSlug, setRepoSlug] = useState("");
  const [teamId, setTeamId] = useState("");
  const [visibility, setVisibility] = useState<AccessVisibility>("code");
  const [denyText, setDenyText] = useState("");
  const [allowText, setAllowText] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [note, setNote] = useState("");

  const upsert = useMutation({
    mutationFn: () =>
      accessApi.upsertRule(token!, {
        team_id: teamId,
        repo_slug: repoSlug.trim(),
        visibility,
        deny_globs: linesToArr(denyText),
        allow_globs: linesToArr(allowText),
        sensitivity_tags: linesToArr(tagsText),
        note: note.trim(),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["access", "rules"] });
      toast.success(t("admin.access.ruleSaved"));
      setRepoSlug(""); setDenyText(""); setAllowText(""); setTagsText(""); setNote("");
    },
    onError: (e) => toast.error(t("admin.access.error", { message: (e as Error).message })),
  });

  const del = useMutation({
    mutationFn: (id: string) => accessApi.deleteRule(token!, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["access", "rules"] });
      toast.success(t("admin.access.ruleDeleted"));
    },
  });

  const byRepo = useMemo(() => {
    const m: Record<string, AccessRule[]> = {};
    for (const r of rules.data ?? []) (m[r.repo_slug] ??= []).push(r);
    return m;
  }, [rules.data]);

  const addPreset = (globs: string[]) =>
    setDenyText((prev) => {
      const cur = linesToArr(prev);
      const merged = Array.from(new Set([...cur, ...globs]));
      return merged.join("\n");
    });

  return (
    <PageShell width="wide">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
          <ShieldHalfIcon className="h-6 w-6" /> {t("admin.access.title")}
        </h1>
        <p className="text-sm text-[var(--color-muted-foreground)] mt-1">
          {t("admin.access.descIntro")} <b>{t("admin.access.descExplore")}</b>{" "}
          {t("admin.access.descVia")} <b>metadata</b> {t("admin.access.descMetadata")}{" "}
          <b>code</b> {t("admin.access.descCode")} <b>Deny-glob</b>
          {t("admin.access.descDenySuffix")} <code>code</code>{" "}
          {t("admin.access.descTail")}
        </p>
      </div>

      <SectionTabs set="team" />

      {/* Create / upsert */}
      <Card>
        <CardHeader>
          <CardTitle>{t("admin.access.addTitle")}</CardTitle>
          <CardDescription>
            {t("admin.access.addDesc")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <Label>{t("admin.access.repoLabel")}</Label>
              {repos.isError ? (
                <Callout tone="danger">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span>
                      {t("common.loadError")}
                      {(repos.error as Error)?.message ? `: ${(repos.error as Error).message}` : ""}
                    </span>
                    <Button size="sm" variant="outline" onClick={() => repos.refetch()}>
                      {t("common.retry")}
                    </Button>
                  </div>
                </Callout>
              ) : !repos.isLoading && (repos.data ?? []).length === 0 ? (
                <p className="text-xs text-[var(--color-muted-foreground)] py-2">
                  {t("admin.access.noRepos")}{" "}
                  <Link href="/repositories" className="underline text-[var(--color-brand)]">
                    {t("admin.access.noReposLink")}
                  </Link>
                </p>
              ) : (
                <Select
                  className="w-full"
                  value={repoSlug}
                  onChange={(v) => setRepoSlug(v)}
                  disabled={repos.isLoading}
                  options={(repos.data ?? []).map((r) => ({
                    value: r.slug,
                    label: r.full_name || r.slug,
                  }))}
                  placeholder={repos.isLoading ? t("admin.access.loading") : t("admin.access.selectOption")}
                />
              )}
            </div>
            <div>
              <Label>{t("admin.access.teamLabel")}</Label>
              <Select
                className="w-full"
                value={teamId}
                onChange={(v) => setTeamId(v)}
                options={(teams.data ?? []).map((team) => ({ value: team.id, label: team.name }))}
                placeholder={t("admin.access.selectOption")}
              />
            </div>
            <div>
              <Label>{t("admin.access.visibilityLabel")}</Label>
              <Select
                className="w-full"
                value={visibility}
                onChange={(v) => setVisibility(v as AccessVisibility)}
                options={[
                  { value: "none", label: t("admin.access.visNone") },
                  { value: "metadata", label: t("admin.access.visMetadata") },
                  { value: "code", label: t("admin.access.visCode") },
                ]}
              />
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <Label>{t("admin.access.denyLabel")}</Label>
              <Textarea
                value={denyText}
                onChange={(e) => setDenyText(e.target.value)}
                placeholder={"**/credentials/**\n**/crypto/**\n**/db/connection*"}
                className="min-h-[90px] font-mono text-xs"
              />
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {Object.entries(DENY_PRESETS).map(([label, globs]) => (
                  <button
                    key={label}
                    type="button"
                    onClick={() => addPreset(globs)}
                    className="rounded border border-[var(--color-border)] px-2 py-0.5 text-[11px] hover:bg-[var(--color-accent)]"
                  >
                    + {t(PRESET_LABEL_KEYS[label])}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <Label>{t("admin.access.allowLabel")}</Label>
              <Textarea
                value={allowText}
                onChange={(e) => setAllowText(e.target.value)}
                placeholder={"src/api/**\nsrc/handlers/**"}
                className="min-h-[90px] font-mono text-xs"
              />
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-[1fr_2fr_auto] items-end">
            <div>
              <Label>{t("admin.access.tagsLabel")}</Label>
              <Input
                value={tagsText}
                onChange={(e) => setTagsText(e.target.value)}
                placeholder="creds, crypto, db"
              />
            </div>
            <div>
              <Label>{t("admin.access.noteLabel")}</Label>
              <Input
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder={t("admin.access.notePlaceholder")}
              />
            </div>
            <Button
              onClick={() => upsert.mutate()}
              disabled={upsert.isPending || !repoSlug.trim() || !teamId}
            >
              <PlusIcon className="h-4 w-4 mr-1" />
              {upsert.isPending ? "…" : t("admin.access.save")}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Existing rules grouped by repo */}
      {rules.isLoading && (
        <div className="text-sm text-[var(--color-muted-foreground)]">{t("admin.access.loading")}</div>
      )}
      {Object.keys(byRepo).length === 0 && !rules.isLoading && (
        <p className="text-sm text-[var(--color-muted-foreground)]">
          {t("admin.access.emptyState")}
        </p>
      )}
      {Object.entries(byRepo).map(([repo, rs]) => (
        <Card key={repo}>
          <CardHeader>
            <CardTitle className="text-base font-mono">{repo}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {rs.map((r) => (
              <div
                key={r.id}
                className="flex items-start justify-between gap-3 rounded-md border border-[var(--color-border)] px-3 py-2"
              >
                <div className="space-y-1">
                  <div className="flex items-center gap-2 text-sm">
                    <Badge className={VIS_BADGE[r.visibility]}>{r.visibility}</Badge>
                    <span className="font-medium">{r.team_name ?? r.team_id}</span>
                  </div>
                  {r.deny_globs.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1 text-[11px] text-[var(--color-muted-foreground)]">
                      <EyeOffIcon className="h-3 w-3" /> {t("admin.access.denyInline")}
                      {r.deny_globs.map((g) => (
                        <code key={g} className="rounded bg-[var(--color-muted)] px-1">{g}</code>
                      ))}
                    </div>
                  )}
                  {r.allow_globs.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1 text-[11px] text-[var(--color-muted-foreground)]">
                      {t("admin.access.allowInline")}
                      {r.allow_globs.map((g) => (
                        <code key={g} className="rounded bg-[var(--color-muted)] px-1">{g}</code>
                      ))}
                    </div>
                  )}
                  {r.note && (
                    <p className="text-[11px] text-[var(--color-muted-foreground)]">{r.note}</p>
                  )}
                </div>
                <Button
                  variant="ghost" size="icon"
                  onClick={async () => {
                    const ok = await confirm({
                      title: t("admin.access.confirmDelete"),
                      confirmLabel: t("common.delete"),
                      danger: true,
                    });
                    if (ok) del.mutate(r.id);
                  }}
                  aria-label={t("admin.access.deleteRuleAria")}
                >
                  <Trash2Icon className="h-4 w-4 text-red-600" />
                </Button>
              </div>
            ))}
          </CardContent>
        </Card>
      ))}
      {dialog}
    </PageShell>
  );
}
