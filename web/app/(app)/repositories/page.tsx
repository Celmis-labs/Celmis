"use client";
import { keepPreviousData, useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { useSession } from "next-auth/react";
import { CpuIcon, ExternalLinkIcon, FolderGit2Icon, GitBranchIcon, Loader2Icon, PlusIcon, RefreshCwIcon, SearchIcon, SparklesIcon, TrashIcon, UsersIcon, ZapIcon } from "lucide-react";
import {
  api,
  branchesApi,
  intelApi,
  llmApi,
  openPullsApi,
  type OpenPull,
  type QueuedReview,
  type RepoAddRequest,
  type RepoBrowseItem,
  type RepoOut,
  type RepoOwnerItem,
  type RepoDeveloperScan,
  unruledApi,
  uploadRepoArchive,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useI18n, useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { relativeTime } from "@/lib/review-stages";
import { useSpotlightOnMount } from "@/lib/tour";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { QueryState } from "@/components/ui/query-state";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Select } from "@/components/ui/select";
import { BranchCombobox, toBranchResult } from "@/components/branch-combobox";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { StatusPill, toRunStatus } from "@/components/ui/status";
import { SkeletonRows } from "@/components/ui/skeleton";
import { Callout } from "@/components/ui/callout";

import { WorkspaceBadge } from "@/components/workspace-badge";
import { RepoFreshness } from "@/components/repo-freshness";
import { RepairOutdatedHooks, RepoWebhookControl } from "@/components/repo-webhook";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";

/** Same set the settings page offers, so the dialog cannot present a language
 *  the workspace default could never be. Kept as a literal rather than fetched
 *  — it is a static list and a spinner here would be worse than a duplicate. */
const VAULT_LANGS = [
  { value: "uk", label: "Українська" }, { value: "en", label: "English" },
  { value: "pl", label: "Polski" }, { value: "de", label: "Deutsch" },
  { value: "fr", label: "Français" }, { value: "es", label: "Español" },
  { value: "pt", label: "Português" }, { value: "it", label: "Italiano" },
  { value: "nl", label: "Nederlands" }, { value: "cs", label: "Čeština" },
  { value: "sk", label: "Slovenčina" }, { value: "ro", label: "Română" },
  { value: "hu", label: "Magyar" }, { value: "bg", label: "Български" },
  { value: "el", label: "Ελληνικά" }, { value: "tr", label: "Türkçe" },
  { value: "sv", label: "Svenska" }, { value: "no", label: "Norsk" },
  { value: "da", label: "Dansk" }, { value: "fi", label: "Suomi" },
  { value: "et", label: "Eesti" }, { value: "lv", label: "Latviešu" },
  { value: "lt", label: "Lietuvių" }, { value: "hr", label: "Hrvatski" },
  { value: "sr", label: "Srpski" }, { value: "ka", label: "ქართული" },
  { value: "he", label: "עברית" }, { value: "ar", label: "العربية" },
  { value: "hi", label: "हिन्दी" }, { value: "vi", label: "Tiếng Việt" },
  { value: "th", label: "ไทย" }, { value: "id", label: "Bahasa Indonesia" },
  { value: "ms", label: "Bahasa Melayu" }, { value: "ja", label: "日本語" },
  { value: "ko", label: "한국어" }, { value: "zh-CN", label: "简体中文" },
  { value: "zh-TW", label: "繁體中文" },
];

type Provider = "github" | "gitlab" | "bitbucket";

type Translate = (key: string, vars?: Record<string, string | number>) => string;

/**
 * The message catalogs are maintained outside this file and the keys for the
 * repo search do not exist there yet — `t()` echoes an unknown key straight
 * back, which would ship literal "repositories.browseSearch" as UI text.
 * English stands in until the keys land, then they take over untouched.
 */
function withFallback(t: Translate) {
  return (key: string, english: string, vars?: Record<string, string | number>) => {
    const translated = t(key, vars);
    if (translated !== key) return translated;
    return english.replace(/\{(\w+)\}/g, (_m, name: string) =>
      vars && name in vars ? String(vars[name]) : `{${name}}`);
  };
}

export default function RepositoriesPage() {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  // Slugs with an index request in flight. Indexing is synchronous on the API
  // but can run for minutes — while it runs we poll the list so the "indexed"
  // badge still flips if the POST itself dies on a proxy timeout.
  const [indexing, setIndexing] = useState<string[]>([]);

  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
    refetchInterval: indexing.length > 0 ? 5_000 : false,
  });

  // Hands back the invalidation's promise on purpose. A repo that was just
  // registered may only be marked "indexing" once the list it is reconciled
  // against has actually refetched — the effect below drops any slug the list
  // does not carry, so marking it first would silently un-mark it and the
  // badge would never move.
  const refresh = useCallback(
    () => qc.invalidateQueries({ queryKey: ["repos"] }),
    [qc],
  );
  const startIndexing = useCallback(
    (slug: string) => setIndexing((prev) => (prev.includes(slug) ? prev : [...prev, slug])),
    [],
  );
  const stopIndexing = useCallback(
    (slug: string) => setIndexing((prev) => prev.filter((s) => s !== slug)),
    [],
  );

  // A queued index that dies (bad credentials, a clone that fails) never makes
  // the repo report `indexed`, so the rule below alone would keep the badge
  // spinning and the 5-second poll running for the rest of the session. Give
  // the whole wait a ceiling — the row falls back to "index now", which is
  // both true and actionable.
  useEffect(() => {
    if (indexing.length === 0) return;
    const id = setTimeout(() => setIndexing([]), 15 * 60_000);
    return () => clearTimeout(id);
  }, [indexing.length]);

  // Stop polling as soon as the list itself reports the repo as indexed (or the
  // repo disappeared), so a lost mutation callback can never leave a 5s poll
  // running forever.
  const repoRows = repos.data;
  useEffect(() => {
    if (!repoRows) return;
    const pending = new Set(
      repoRows.filter((r) => !r.indexed).map((r) => r.slug),
    );
    setIndexing((prev) => {
      const next = prev.filter((s) => pending.has(s));
      return next.length === prev.length ? prev : next;
    });
  }, [repoRows]);

  // Onboarding "show me where" lands here for add-repo / generate-vault.
  useSpotlightOnMount();

  return (
    <PageShell width="wide">
      <PageHeader
        title={t("repositories.title")}
        badge={<WorkspaceBadge />}
        description={t("repositories.subtitle")}
        tabs={<SectionTabs set="sources" />}
      />

      <div className="grid gap-6 lg:grid-cols-[1fr_400px]">
        <ConnectedRepoList
          query={repos}
          onChange={refresh}
          indexing={indexing}
          onIndexStart={startIndexing}
          onIndexEnd={stopIndexing}
        />
        <AddRepoCard onAdded={refresh} onIndexStart={startIndexing} />
      </div>
    </PageShell>
  );
}

function ConnectedRepoList({
  query, onChange, indexing, onIndexStart, onIndexEnd,
}: {
  query: UseQueryResult<RepoOut[]>;
  onChange: () => void;
  indexing: string[];
  onIndexStart: (slug: string) => void;
  onIndexEnd: (slug: string) => void;
}) {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const repoList = query.data ?? [];
  const unindexed = repoList.filter((r) => !r.indexed).length;

  // Queued, not synchronous: nine repos at 5-60s each is far past a request's
  // patience. The queue gives retries and a cancel button, and the list below
  // already polls while anything is indexing.
  const indexAll = useMutation({
    mutationFn: async () =>
      api<{ queued: number; skipped: number; already_indexed: string[] }>(
        "/api/repos/index-all", { method: "POST", token }),
    onSuccess: (r) => {
      // "queued === 0" has two very different causes and they used to share
      // one message: nothing needed doing, versus every job was rejected
      // because an earlier index for the same repo is still in the queue.
      if (r.queued > 0) {
        toast.success(`${r.queued} queued for indexing.`);
      } else if (r.skipped > 0) {
        toast.info(`${r.skipped} already queued from an earlier run — nothing new to add.`);
      } else {
        toast.info(t("repos.allIndexed"));
      }
      // Only the repos whose jobs were actually accepted get the spinner. A
      // repo marked "indexing" that nothing will ever un-mark leaves the list
      // polling every 5 seconds forever.
      if (r.queued > 0) {
        repoList.filter((x) => !x.indexed).forEach((x) => onIndexStart(x.slug));
      }
      void qc.invalidateQueries({ queryKey: ["repos"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle>{t("repositories.connectedTitle")}</CardTitle>
            <CardDescription>
              {t("repositories.connectedDescription")}
            </CardDescription>
          </div>
          {repoList.length > 0 && (
            <Button size="sm" variant="outline" className="h-11 shrink-0 sm:h-8"
              disabled={indexAll.isPending || unindexed === 0}
              onClick={() => indexAll.mutate()}>
              {indexAll.isPending
                ? <Loader2Icon className="mr-1 h-3.5 w-3.5 animate-spin" />
                : <CpuIcon className="mr-1 h-3.5 w-3.5" />}
              {unindexed === 0 ? "All indexed" : `Index all (${unindexed})`}
            </Button>
          )}
        </div>
        {/* Said out loud because the two are constantly confused: an index is
            the graph chat and search answer from; a vault is generated prose
            that costs model calls and is never required for either. */}
        <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
          Builds the code graph only — no documentation is generated and no
          model calls are made.
        </p>
      </CardHeader>
      <CardContent>
        <RepairOutdatedHooks repos={repoList} />
        <QueryState
          query={query}
          empty={{ icon: FolderGit2Icon, title: t("repositories.emptyList") }}
        >
          {(repos) => (
            <ul className="flex flex-col gap-3">
              {repos.map((r) => (
                <RepoRow
                  key={r.slug}
                  repo={r}
                  onChange={onChange}
                  isIndexing={indexing.includes(r.slug)}
                  onIndexStart={onIndexStart}
                  onIndexEnd={onIndexEnd}
                />
              ))}
            </ul>
          )}
        </QueryState>
      </CardContent>
    </Card>
  );
}

function RepoRow({
  repo, onChange, isIndexing, onIndexStart, onIndexEnd,
}: {
  repo: RepoOut;
  onChange: () => void;
  isIndexing: boolean;
  onIndexStart: (slug: string) => void;
  onIndexEnd: (slug: string) => void;
}) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [showPRs, setShowPRs] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [purge, setPurge] = useState(false);
  // Only a workspace admin gets this list (the endpoint refuses everyone
  // else), which is exactly who the badge is for.
  const unruled = useQuery({
    queryKey: ["access", "unruled"],
    queryFn: () => unruledApi.list(token!),
    enabled: !!token,
    retry: false,
  });
  const adminsOnly = !!unruled.data?.repos.includes(repo.slug);

  const remove = useMutation({
    mutationFn: async (purge: boolean) =>
      api<void | Record<string, unknown>>(
        `/api/repos/${repo.slug}${purge ? "?purge=true" : ""}`,
        { method: "DELETE", token },
      ),
    onSuccess: (result, purge) => {
      if (purge && result && typeof result === "object") {
        const r = result as {
          qdrant_points_deleted: number;
          disk_bytes_freed: number;
          group_memberships_removed: number;
          errors: string[];
        };
        const mb = (r.disk_bytes_freed / (1024 * 1024)).toFixed(1);
        toast.success(
          t("repositories.purgedSuccess", { mb, points: r.qdrant_points_deleted }) +
          (r.errors?.length ? t("repositories.purgedErrors", { count: r.errors.length }) : ""),
        );
      } else {
        toast.success(t("repositories.removedLightweight"));
      }
      onChange();
    },
    onError: (e: Error) => toast.error(t("repositories.removeFailed", { message: e.message })),
  });

  const indexNow = useMutation({
    mutationFn: async () =>
      api<RepoOut>(`/api/repos/${repo.slug}/index`, { method: "POST", token }),
    onMutate: () => onIndexStart(repo.slug),
    onSuccess: (updated) => {
      // Patch the row straight into the list cache so the badge flips on the
      // same tick, then invalidate for the authoritative state. Relying on the
      // invalidate alone left the button reading "index now" until a manual
      // reload whenever the refetch raced the response.
      qc.setQueryData<RepoOut[]>(["repos"], (old) =>
        old?.map((r) => (r.slug === repo.slug ? { ...r, ...updated } : r)));
      toast.success(t("repositories.indexedSuccess"));
      onChange();
    },
    onError: (e: Error) => toast.error(t("repositories.indexFailed", { message: e.message })),
    onSettled: () => onIndexEnd(repo.slug),
  });
  const indexBusy = indexNow.isPending || isIndexing;

  const rebuildOwnership = useMutation({
    mutationFn: async () => intelApi.rebuildOwnership(token!, repo.slug, 90),
    onSuccess: (r) => {
      toast.success(t("repositories.ownershipRebuilt", { snapshot: r.snapshot_id.slice(0, 8) }));
    },
    onError: (e: Error) => toast.error(t("repositories.ownershipRebuildFailed", { message: e.message })),
  });

  // The language this build writes in. Seeded from the workspace setting, so
  // the common case is confirm-and-go; overriding it here is deliberately NOT
  // persisted — "generate this one in English for the customer" should not
  // change a setting somebody then has to remember to change back, and a
  // sticky override is an invisible setting.
  const [vaultOpen, setVaultOpen] = useState(false);
  const [vaultLang, setVaultLang] = useState<string | null>(null);
  const [vaultEngine, setVaultEngine] = useState<string | null>(null);
  const llmConfig = useQuery({
    queryKey: ["llm-config"],
    queryFn: () => llmApi.getConfig(token!),
    enabled: !!token && vaultOpen,
  });
  const wsLang = llmConfig.data?.docs_language ?? "uk";
  const effectiveLang = vaultLang ?? wsLang;
  const wsEngine = llmConfig.data?.docs_engine ?? "api";
  const effectiveEngine = vaultEngine ?? wsEngine;

  const generateVault = useMutation({
    mutationFn: async () =>
      api<{ detail: string; language?: string; queued?: boolean }>(
        `/api/repos/${repo.slug}/generate-vault`,
        { method: "POST", token,
          json: { language: effectiveLang, engine: effectiveEngine } },
      ),
    onSuccess: (r) => {
      // Only close on a build that actually started. A deduped request closed
      // the dialog and toasted success, so the second press looked identical
      // to the first.
      if (r.queued === false) { toast.info(r.detail); return; }
      setVaultOpen(false); setVaultLang(null); setVaultEngine(null);
      toast.success(r.detail);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <li className="rounded-lg border border-[var(--color-border)] p-4 hover:bg-[var(--color-accent)]/30 transition-colors">
      <div className="flex items-start justify-between gap-2 sm:items-center sm:gap-4">
        <div className="flex items-start gap-3 min-w-0 flex-1 sm:items-center">
          <ProviderBadge provider={repo.provider as Provider} />
          <div className="min-w-0">
            <div className="font-medium wrap-anywhere sm:truncate">{repo.full_name}</div>
            {/* Wraps instead of widening the card: on a phone the badge, the
                two work buttons, the branch chip and the link never fit one line. */}
            {/* Freshness first, because "is this current?" is the question a
                person came to ask; everything below it is what to DO about
                the answer. */}
            {repo.indexed && (
              <div className="mt-1 text-xs">
                <RepoFreshness repo={repo} token={token} />
              </div>
            )}
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
              {adminsOnly && (
                <Badge variant="warning" className="px-1.5 py-0" title={t("repositories.adminsOnlyHint")}>
                  {t("repositories.adminsOnlyBadge")}
                </Badge>
              )}
              {repo.indexed ? (
                <>
                  <Badge variant="success" className="px-1.5 py-0">{t("repositories.indexedBadge")}</Badge>
                  <Dialog open={vaultOpen} onOpenChange={setVaultOpen}>
                    <DialogTrigger asChild>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        data-tour="generate-vault"
                        disabled={generateVault.isPending}
                        title={t("repositories.generateVaultTitle")}
                      >
                        <CpuIcon className="h-3.5 w-3.5" />
                        {generateVault.isPending ? t("repositories.generatingVault") : t("repositories.generateVault")}
                      </Button>
                    </DialogTrigger>
                    <DialogContent>
                      <DialogHeader>
                        <DialogTitle>{t("repositories.vaultDialogTitle")}</DialogTitle>
                        {/* Says what is about to happen. This was only ever in
                            a toast AFTER the job was queued, which is the one
                            moment the information is no longer useful. */}
                        <DialogDescription>
                          {t("repositories.vaultDialogBody", { repo: repo.full_name })}
                        </DialogDescription>
                      </DialogHeader>
                      <div className="space-y-2">
                        <Label>{t("repositories.vaultLanguageLabel")}</Label>
                        <Select
                          className="w-full"
                          value={effectiveLang}
                          onChange={(v) => setVaultLang(v)}
                          options={VAULT_LANGS}
                        />
                        <p className="text-xs text-[var(--color-muted-foreground)]">
                          {vaultLang && vaultLang !== wsLang
                            ? t("repositories.vaultLanguageOverride")
                            : t("repositories.vaultLanguageDefault")}
                        </p>
                        <Label className="pt-2">{t("repositories.vaultEngineLabel")}</Label>
                        <Select
                          className="w-full"
                          value={effectiveEngine}
                          onChange={(v) => setVaultEngine(v)}
                          options={[
                            { value: "api", label: t("repositories.vaultEngineApi") },
                            { value: "claude_code", label: t("repositories.vaultEngineAgent") },
                          ]}
                        />
                        {/* The trade-off stated where the choice is made. The
                            agent is slower and needs a Claude connection; it
                            is also the only one that can look anything up. */}
                        <p className="text-xs text-[var(--color-muted-foreground)]">
                          {effectiveEngine === "claude_code"
                            ? t("repositories.vaultEngineAgentHint")
                            : t("repositories.vaultEngineApiHint")}
                        </p>
                      </div>
                      <DialogFooter>
                        <Button
                          type="button"
                          onClick={() => generateVault.mutate()}
                          disabled={generateVault.isPending}
                        >
                          {generateVault.isPending
                            ? t("repositories.generatingVault")
                            : t("repositories.vaultDialogConfirm")}
                        </Button>
                      </DialogFooter>
                    </DialogContent>
                  </Dialog>
                </>
              ) : indexBusy ? (
                <Badge variant="outline" className="gap-1 px-1.5 py-0">
                  <Loader2Icon className="h-3 w-3 animate-spin" />
                  {t("repositories.indexing")}
                </Badge>
              ) : (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => indexNow.mutate()}
                  title={t("repositories.indexNowTitle")}
                >
                  <CpuIcon className="h-3.5 w-3.5" />
                  {t("repositories.indexNow")}
                </Button>
              )}
              <BranchPicker repo={repo} onChange={onChange} />
              {/* Without a webhook nothing is ever reviewed automatically, and
                  nothing else on this page would say so. */}
              <RepoWebhookControl repo={repo} />
              <a
                href={repo.url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex min-h-9 items-center gap-0.5 hover:underline"
              >
                {t("repositories.openLink")} <ExternalLinkIcon className="h-3 w-3" />
              </a>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-1 shrink-0 sm:gap-3">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => setShowPRs((v) => !v)}
            title={t("repositories.openPrsTitle")}
          >
            <ZapIcon className="h-4 w-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            onClick={() => rebuildOwnership.mutate()}
            disabled={rebuildOwnership.isPending || !repo.indexed}
            title={repo.indexed
              ? t("repositories.rebuildOwnershipTitle")
              : t("repositories.needsIndexFirst")}
          >
            <UsersIcon className="h-4 w-4" />
          </Button>
          <Dialog open={removeOpen} onOpenChange={setRemoveOpen}>
            <DialogTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                title={t("repositories.removeTitle")}
                disabled={remove.isPending}
              >
                <TrashIcon className="h-4 w-4" />
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>{t("repositories.removeTitle")}</DialogTitle>
                <DialogDescription>
                  {t("repositories.confirmRemove", { name: repo.full_name })}
                </DialogDescription>
              </DialogHeader>
              {/* Plain div, not a label — Switch renders a <button>, which is a
                  labelable element, so a wrapping label would forward the click
                  and toggle twice. */}
              <div className="flex items-start gap-3 rounded-md border border-[var(--color-border)] p-3 text-sm">
                <Switch checked={purge} onCheckedChange={setPurge} />
                <div>
                  <div className="font-medium">{t("repositories.purgeLabel")}</div>
                  <p className="text-xs text-[var(--color-muted-foreground)] mt-0.5">
                    {t("repositories.confirmPurge")}
                  </p>
                </div>
              </div>
              <DialogFooter>
                <Button variant="ghost" onClick={() => setRemoveOpen(false)}>
                  {t("common.cancel")}
                </Button>
                <Button
                  variant="destructive"
                  disabled={remove.isPending}
                  onClick={() => {
                    setRemoveOpen(false);
                    remove.mutate(purge);
                  }}
                >
                  {remove.isPending
                    ? t("repositories.removing")
                    : purge ? t("repositories.purgeConfirmButton") : t("repositories.removeButton")}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      {showPRs && <ManualPullList slug={repo.slug} repo={repo} />}
    </li>
  );
}

/**
 * Current branch as a badge; click to switch it.
 *
 * The branch list comes from the provider (GET /api/repos/{slug}/branches),
 * which is a real API round-trip per repo — so it is only fetched once the
 * user actually opens the editor, never for every row on page load. The
 * combobox searches the server's full listing, not just its first page.
 */
function BranchPicker({ repo, onChange }: { repo: RepoOut; onChange: () => void }) {
  const token = useToken();
  const t = useT();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);

  const save = useMutation({
    mutationFn: async (branch: string) =>
      api<RepoOut>(`/api/repos/${repo.slug}/branch`, {
        method: "PATCH",
        token,
        json: { branch: branch || null },
      }),
    onSuccess: (updated) => {
      qc.setQueryData<RepoOut[]>(["repos"], (old) =>
        old?.map((r) => (r.slug === repo.slug ? { ...r, ...updated } : r)));
      setEditing(false);
      // The local clone still holds the OLD ref until the next index run —
      // say so, otherwise the next Q&A answer silently comes from stale code.
      toast.success(
        t("repositories.branchChanged", {
          branch: updated.branch || t("repositories.branchDefault"),
        }),
        { description: t("repositories.branchReindexHint") },
      );
      onChange();
    },
    onError: (e: Error) => toast.error(t("repositories.branchChangeFailed", { message: e.message })),
  });

  if (!editing) {
    return (
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={() => setEditing(true)}
        title={t("repositories.branchChangeTitle")}
        className="max-w-[12rem]"
      >
        <GitBranchIcon className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate font-mono">
          {repo.branch || t("repositories.branchDefault")}
        </span>
      </Button>
    );
  }

  // The saved branch stays the shown value even when the provider list fails
  // to load (no token, API hiccup) — opening the editor must never be able to
  // reset the repo to its default branch on its own.
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <BranchCombobox
        value={repo.branch ?? ""}
        onChange={(v) => save.mutate(v)}
        search={(q) => branchesApi.forRepo(token!, repo.slug, q).then(toBranchResult)}
        queryKey={["repo-branches", repo.slug]}
        leadingOptions={[{ value: "", label: t("repositories.branchDefault") }]}
        disabled={save.isPending || !token}
        defaultOpen
        onClose={() => { if (!save.isPending) setEditing(false); }}
        ariaLabel={t("repositories.branchChangeTitle")}
        className="h-9 max-w-[16rem] rounded border-[var(--color-input)] px-2 text-sm sm:h-7 sm:text-xs"
      />
      {save.isPending && <Loader2Icon className="h-3 w-3 animate-spin" />}
    </span>
  );
}

const PR_PAGE = 25;

/** The list of open PRs a person reviews by hand: every open PR of the
 *  repository whatever branch it targets, all provider pages, searchable,
 *  each with its last Celmis review. "Review" queues one; "Review all open
 *  PRs" queues every PR the filters show (at most `bulk_limit`), after a
 *  confirmation, through the same queue — so each run records its stages. */
function ManualPullList({ slug, repo }: { slug: string; repo: RepoOut }) {
  const token = useToken();
  const { t, locale } = useI18n();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();
  const [branch, setBranch] = useState<string>("");
  const [sort, setSort] = useState<"newest" | "recently_updated" | "oldest">("newest");
  const [q, setQ] = useState("");
  const [targetedOnly, setTargetedOnly] = useState<"" | "true">("");
  const [offset, setOffset] = useState(0);

  const filters = {
    q: q.trim(), branch, sort, limit: PR_PAGE, offset, targeted_only: targetedOnly,
  };
  const prs = useQuery({
    queryKey: ["pulls", slug, filters],
    queryFn: () => openPullsApi.list(token!, slug, filters),
    enabled: !!token,
    placeholderData: keepPreviousData,
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["pulls", slug] });

  const answer = (r: QueuedReview) => {
    if (r.status === "duplicate") toast.info(t("repositories.reviewDuplicate"));
    else if (r.status === "failed") toast.error(r.reason || t("repositories.reviewFailed"));
    else toast.success(t("repositories.reviewQueued", { id: (r.run_id ?? "").slice(0, 8) }));
  };
  const trigger = useMutation({
    mutationFn: async (pr: OpenPull) => openPullsApi.review(token!, slug, pr.number),
    onSuccess: (r) => { answer(r); refresh(); },
    onError: (e: Error) => toast.error(e.message),
  });
  const bulk = useMutation({
    mutationFn: async () => openPullsApi.reviewAll(token!, slug, {
      q: q.trim(), branch: branch || null, confirm: true,
    }),
    onSuccess: (r) => {
      const left = r.skipped?.length ?? 0;
      toast.success(
        t("repositories.reviewAllQueued", { queued: r.queued, requested: r.requested })
        + (left ? ` ${t("repositories.reviewAllSkippedNote", { n: left })}` : ""),
      );
      refresh();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const data = prs.data;
  const limit = data?.bulk_limit ?? 25;
  // "Review all" queues only PRs on a targeted base branch; the others would
  // be skipped by the orchestrator's gate, so they are not counted here.
  const reviewable = data?.targeted_total ?? 0;
  const patterns = (data?.effective_target_branches ?? []).join(", ");
  const tooMany = reviewable > limit;
  const onReviewAll = async () => {
    if (!data || reviewable === 0 || tooMany) return;
    const ok = await confirm({
      title: t("repositories.reviewAllTitle", { n: reviewable }),
      description: t("repositories.reviewAllDesc"),
      confirmLabel: t("repositories.reviewAllConfirm"),
    });
    if (ok) bulk.mutate();
  };
  const pick = <V,>(set: (v: V) => void) => (v: V) => { set(v); setOffset(0); };
  // The gate stays the authority; this only warns before a review that it
  // would skip is queued by hand.
  const onReview = async (pr: OpenPull) => {
    if (!pr.targeted) {
      const ok = await confirm({
        title: t("repositories.untargetedTitle"),
        description: t("repositories.untargetedDesc", {
          branch: pr.target_branch ?? "?", patterns,
        }),
        confirmLabel: t("repositories.untargetedConfirm"),
      });
      if (!ok) return;
    }
    trigger.mutate(pr);
  };

  return (
    <div className="mt-3 sm:pl-12">
      {dialog}
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/40 p-3">
        <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-baseline gap-x-2 text-sm font-semibold">
            {repo.provider === "gitlab" ? t("repositories.openMergeRequests") : t("repositories.openPullRequests")}
            {data && (
              <span className="text-xs font-normal tabular-nums text-[var(--color-muted-foreground)]">
                {t("repositories.prCount", { shown: data.total, total: data.open_total })}
              </span>
            )}
          </div>
          {/* Every row has its own Review, so the bulk action is the
              neutral solid one and no button on this list is filled teal:
              the list is a set of equal choices, not one obvious step. */}
          <Button
            size="sm"
            variant="secondary"
            disabled={!data || reviewable === 0 || tooMany || bulk.isPending}
            title={tooMany ? t("repositories.reviewAllTooMany", { n: reviewable, max: limit }) : undefined}
            onClick={onReviewAll}
          >
            <SparklesIcon />
            {t("repositories.reviewAll", { n: Math.min(reviewable, limit) })}
          </Button>
        </div>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <Input
            aria-label={t("repositories.prSearch")}
            placeholder={t("repositories.prSearch")}
            value={q}
            onChange={(e) => pick(setQ)(e.target.value)}
            className="h-9 max-w-[18rem] text-sm sm:h-8 sm:text-xs"
          />
          <BranchCombobox
            value={branch}
            onChange={pick(setBranch)}
            search={(term) => branchesApi.forRepo(token!, slug, term).then(toBranchResult)}
            queryKey={["repo-branches", slug]}
            leadingOptions={[{ value: "", label: t("repositories.allTargetBranches") }]}
            disabled={!token}
            className="h-9 max-w-[16rem] rounded-lg border-[var(--color-input)] bg-[var(--color-card)] px-2 text-sm sm:h-8 sm:text-xs"
          />
          <Select
            value={sort}
            onChange={(v) => pick(setSort)(v as typeof sort)}
            options={[
              { value: "newest", label: t("repositories.sortNewest") },
              { value: "recently_updated", label: t("repositories.sortRecentlyUpdated") },
              { value: "oldest", label: t("repositories.sortOldest") },
            ]}
            className="h-9 px-2 text-sm sm:h-8 sm:text-xs"
          />
          {patterns && (
            <Select
              value={targetedOnly}
              onChange={(v) => pick(setTargetedOnly)(v as "" | "true")}
              options={[
                { value: "", label: t("repositories.targetFilterAll") },
                { value: "true", label: t("repositories.targetFilterTargeted") },
              ]}
              className="h-9 px-2 text-sm sm:h-8 sm:text-xs"
            />
          )}
        </div>
        {tooMany && (
          <p className="mb-2 text-xs text-[var(--color-muted-foreground)]">
            {t("repositories.reviewAllTooMany", { n: reviewable, max: limit })}
          </p>
        )}
        {data?.truncated && (
          <p className="mb-2 text-xs text-[var(--color-warning)]">
            {t("repositories.prTruncated", { n: data.open_total })}
          </p>
        )}
        {prs.isLoading ? (
          <SkeletonRows rows={3} />
        ) : prs.error ? (
          <Callout tone="danger">{(prs.error as Error).message}</Callout>
        ) : (data?.items.length ?? 0) === 0 ? (
          <div className="rounded-md border border-dashed border-[var(--color-border-strong)] px-3 py-6 text-center text-sm text-[var(--color-muted-foreground)]">
            {t("repositories.noOpenPrs")}
          </div>
        ) : (
          <>
            <ul className="divide-y divide-[var(--color-border)] overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-card)]">
              {/* Row on desktop, stacked card on a phone — the title plus two
                  actions cannot share a 390px line without overflowing. */}
              {data!.items.map((pr) => (
                <li
                  key={`${pr.provider}-${pr.number}`}
                  className="flex flex-col gap-2 px-3 py-2.5 transition-colors hover:bg-[var(--color-accent)]/60 sm:flex-row sm:items-center sm:justify-between"
                >
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="shrink-0 font-mono text-xs text-[var(--color-muted-foreground)]">
                        #{pr.number}
                      </span>
                      <span className="truncate text-sm font-medium">{pr.title}</span>
                      {pr.draft && (
                        <Badge variant="outline">{t("repositories.draft")}</Badge>
                      )}
                    </div>
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-[var(--color-muted-foreground)]">
                      <span className="max-w-[22rem] truncate font-mono"
                        title={`${pr.target_branch ?? "?"} ← ${pr.source_branch ?? "?"}`}>
                        {pr.target_branch ?? "?"} ← {pr.source_branch ?? "?"}
                      </span>
                      {pr.author && <span>· {pr.author}</span>}
                      <span title={formatDateTime(pr.updated_at)}>
                        · {t("repositories.prUpdated", { time: relativeTime(pr.updated_at ?? pr.created_at, locale) })}
                      </span>
                      {!pr.targeted && (
                        <Badge variant="outline"
                          title={t("repositories.notTargetedHint", {
                            branch: pr.target_branch ?? "?", patterns,
                          })}>
                          {t("repositories.notTargeted")}
                        </Badge>
                      )}
                      {pr.last_review_status ? (
                        <StatusPill
                          status={toRunStatus(pr.last_review_status)}
                          title={pr.last_review_reason ?? undefined}
                          label={<>{t("repositories.lastReview")}: {t(`prs.review.${pr.last_review_status}`)}</>}
                        />
                      ) : (
                        <span className="text-[var(--color-subtle-foreground)]">· {t("repositories.notReviewed")}</span>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center justify-end gap-2 sm:shrink-0">
                    <a
                      href={pr.url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex min-h-9 items-center gap-0.5 text-xs text-[var(--color-muted-foreground)] hover:underline"
                    >
                      {t("repositories.viewLink")} <ExternalLinkIcon className="h-3 w-3" />
                    </a>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={trigger.isPending}
                      loading={trigger.isPending && trigger.variables?.number === pr.number}
                      onClick={() => onReview(pr)}
                    >
                      <SparklesIcon />
                      {t("repositories.reviewButton")}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
            {data!.total > PR_PAGE && (
              <div className="mt-2 flex items-center justify-between text-xs tabular-nums text-[var(--color-muted-foreground)]">
                <span>
                  {t("issues.range", {
                    from: data!.offset + 1,
                    to: data!.offset + data!.items.length,
                    total: data!.total,
                  })}
                </span>
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - PR_PAGE))}>
                    {t("repositories.prev")}
                  </Button>
                  <Button size="sm" variant="outline"
                    disabled={data!.offset + data!.items.length >= data!.total}
                    onClick={() => setOffset(offset + PR_PAGE)}>
                    {t("repositories.next")}
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function AddRepoCard({ onAdded, onIndexStart }: {
  onAdded: () => Promise<unknown>;
  onIndexStart: (slug: string) => void;
}) {
  const t = useT();
  const { data: session } = useSession();
  const canUpload = Boolean(session?.isSuperadmin);
  return (
    <Card data-tour="add-repo">
      <CardHeader>
        <CardTitle>{t("repositories.addRepoTitle")}</CardTitle>
        <CardDescription>
          {t("repositories.addRepoDescription")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="url">
          <TabsList className={`grid w-full ${canUpload ? "grid-cols-3" : "grid-cols-2"}`}>
            <TabsTrigger value="url">{t("repositories.tabByUrl")}</TabsTrigger>
            <TabsTrigger value="browse">{t("repositories.tabBrowse")}</TabsTrigger>
            {canUpload && (
              <TabsTrigger value="archive">{t("repositories.tabArchive")}</TabsTrigger>
            )}
          </TabsList>
          <TabsContent value="url">
            <AddByUrl onAdded={onAdded} onIndexStart={onIndexStart} />
          </TabsContent>
          <TabsContent value="browse">
            <BrowseRepos onAdded={onAdded} onIndexStart={onIndexStart} />
          </TabsContent>
          {canUpload && (
            <TabsContent value="archive">
              <UploadArchive onAdded={onAdded} onIndexStart={onIndexStart} />
            </TabsContent>
          )}
        </Tabs>
      </CardContent>
    </Card>
  );
}

function UploadArchive({ onAdded, onIndexStart }: {
  onAdded: () => Promise<unknown>;
  onIndexStart: (slug: string) => void;
}) {
  const token = useToken();
  const t = useT();
  const [progress, setProgress] = useState<number | null>(null);
  const [abort, setAbort] = useState<AbortController | null>(null);
  const upload = useMutation({
    mutationFn: async (v: { slug: string; name: string; file: File }) => {
      const ctl = new AbortController();
      setAbort(ctl);
      setProgress(0);
      try {
        return await uploadRepoArchive(token!, v, {
          onProgress: setProgress, signal: ctl.signal,
        });
      } finally {
        setAbort(null);
        setProgress(null);
      }
    },
    onSuccess: async (r) => {
      toast.success(t("repositories.archiveDone"));
      await onAdded();
      if (indexInFlight(r)) onIndexStart(r.slug);
    },
    onError: (e: Error) => {
      if (e.name !== "AbortError") toast.error(e.message);
    },
  });

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        const fd = new FormData(e.currentTarget);
        const file = fd.get("file");
        const slug = String(fd.get("slug") || "").trim();
        if (file instanceof File && file.size > 0 && slug) {
          upload.mutate({ slug, name: String(fd.get("name") || "").trim(), file });
        }
      }}
    >
      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("repositories.archiveHint")}
      </p>
      <div>
        <Label htmlFor="archive-slug">{t("repositories.archiveSlug")}</Label>
        <Input id="archive-slug" name="slug" required pattern="[A-Za-z0-9._-]+" />
      </div>
      <div>
        <Label htmlFor="archive-name">{t("repositories.archiveName")}</Label>
        <Input id="archive-name" name="name" />
      </div>
      <div>
        <Label htmlFor="archive-file">{t("repositories.archiveFile")}</Label>
        <Input id="archive-file" name="file" type="file" required
          accept=".zip,.tar.gz,.tgz" />
      </div>
      {progress !== null && (
        <div role="progressbar" aria-valuenow={Math.round(progress * 100)}
          aria-valuemin={0} aria-valuemax={100}
          className="h-2 w-full overflow-hidden rounded bg-[var(--color-muted)]">
          <div className="h-full bg-[var(--color-primary)] transition-all"
            style={{ width: `${Math.round(progress * 100)}%` }} />
        </div>
      )}
      <div className="flex gap-2">
        <Button type="submit" disabled={upload.isPending || !token}>
          <PlusIcon className="h-4 w-4" />
          {upload.isPending ? t("repositories.archiveUploading") : t("repositories.archiveUpload")}
        </Button>
        {abort && (
          <Button type="button" variant="outline" onClick={() => abort.abort()}>
            {t("repositories.archiveCancel")}
          </Button>
        )}
      </div>
    </form>
  );
}

/** Will this repo's badge move on its own?
 *
 *  Both "we queued one just now" and "one was already queued" mean a worker
 *  is going to write the graph, so both start the 5-second poll. The other
 *  three statuses mean nothing is running and polling would spin forever. */
function indexInFlight(repo: RepoOut): boolean {
  return repo.index_status === "queued" || repo.index_status === "already_queued";
}

/** What was added, AND what that did about its graph.
 *
 *  Saying only "Added" is the bug this screen is being fixed for: fifty
 *  repositories were registered, every one of them reported success, none of
 *  them was ever cloned, and the reviews that followed ran with no graph. */
function addedMessage(t: Translate, repo: RepoOut, name: string): string {
  switch (repo.index_status) {
    case "queued":
      return t("repositories.addedIndexing", { name });
    case "already_queued":
      return t("repositories.addedIndexPending", { name });
    case "already_indexed":
      return t("repositories.addedIndexed", { name });
    default:
      // not_requested / queue_unavailable / a server too old to answer.
      return t("repositories.addedNotIndexed", { name });
  }
}

function AddByUrl({ onAdded, onIndexStart }: {
  onAdded: () => Promise<unknown>;
  onIndexStart: (slug: string) => void;
}) {
  const token = useToken();
  const t = useT();
  const add = useMutation({
    mutationFn: async (payload: { url: string; branch: string | null }) => {
      const body: RepoAddRequest = {
        url: payload.url, auto_review: false, branch: payload.branch,
      };
      return api<RepoOut>("/api/repos", { method: "POST", token, json: body });
    },
    onSuccess: async (r) => {
      toast[indexInFlight(r) || r.index_status === "already_indexed"
        ? "success" : "info"](addedMessage(t, r, r.full_name));
      await onAdded();
      if (indexInFlight(r)) onIndexStart(r.slug);
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        const fd = new FormData(e.currentTarget);
        const url = String(fd.get("url") || "").trim();
        const branch = String(fd.get("branch") || "").trim();
        if (url) add.mutate({ url, branch: branch || null });
      }}
    >
      <div>
        <Label htmlFor="repo-url">{t("repositories.urlOrSlug")}</Label>
        <Input
          id="repo-url"
          name="url"
          placeholder="https://github.com/owner/repo"
          required
        />
        <p className="text-xs text-[var(--color-muted-foreground)] mt-1">
          {t("repositories.urlHint")} {t("repositories.urlHintSelfHosted")}
        </p>
      </div>
      {/* Free text, not a Select: the repo does not exist for us yet, so
          there is no branch list to fetch until after it is added. */}
      <div>
        <Label htmlFor="repo-branch">{t("repositories.branchLabel")}</Label>
        <Input
          id="repo-branch"
          name="branch"
          placeholder={t("repositories.branchPlaceholder")}
        />
        <p className="text-xs text-[var(--color-muted-foreground)] mt-1">
          {t("repositories.branchAddHint")}
        </p>
      </div>
      <Button type="submit" disabled={add.isPending}>
        <PlusIcon className="h-4 w-4" />
        {add.isPending ? t("repositories.adding") : t("repositories.addButton")}
      </Button>
    </form>
  );
}

const BROWSE_PAGE_SIZE = 20;

/** The `owner/` part of what is typed in the browse filter, or "". */
function ownerOf(search: string): string {
  const i = search.lastIndexOf("/");
  return i < 0 ? "" : search.slice(0, i);
}

/** What is typed minus any `owner/` prefix. */
function bareName(search: string): string {
  const i = search.lastIndexOf("/");
  return i < 0 ? search : search.slice(i + 1);
}

function BrowseRepos({ onAdded, onIndexStart }: {
  onAdded: () => Promise<unknown>;
  onIndexStart: (slug: string) => void;
}) {
  const token = useToken();
  const t = useT();
  const tf = withFallback(t);
  const [provider, setProvider] = useState<Provider>("github");
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");

  // The provider does the searching (see the browse endpoint): an account with
  // dozens of repos hides the wanted one on page 4, where a filter over the
  // loaded page would never find it. Debounced, because otherwise every
  // keystroke would spend a provider API call — and GitHub's is the expensive
  // one, it scans several pages per query.
  const typed = search.trim();
  useEffect(() => {
    if (typed === query) return;
    const id = setTimeout(() => {
      setQuery(typed);
      // The query re-slices the whole result set, so whatever page 3 meant a
      // moment ago it does not mean now.
      setPage(1);
    }, 350);
    return () => clearTimeout(id);
  }, [typed, query]);

  // Owners the token can actually see. Typing one by hand is the step people
  // get wrong — a Bitbucket workspace id is not its display name, and an org
  // is not the user who belongs to it — so the ones that exist are offered.
  const owners = useQuery({
    queryKey: ["browse-owners", provider],
    queryFn: () => api<RepoOwnerItem[]>(`/api/repos/browse/${provider}/owners`, { token }),
    enabled: !!token,
    // The scan pages the provider; it must not re-run on every tab focus.
    staleTime: 5 * 60_000,
    retry: false,
  });

  // Who writes the code, not whose account it sits under. One provider request
  // per repository, so this runs only when asked for — never on page load.
  const [devScanOn, setDevScanOn] = useState(false);
  const [showBots, setShowBots] = useState(false);
  // Several developers at once: "everything the backend team touches" is one
  // question, not four.
  const [developer, setDeveloper] = useState<string[]>([]);
  const devs = useQuery({
    queryKey: ["browse-developers", provider],
    queryFn: () =>
      api<RepoDeveloperScan>(`/api/repos/browse/${provider}/developers`, { token }),
    enabled: !!token && devScanOn,
    staleTime: 30 * 60_000,
    retry: false,
  });
  const botCount = (devs.data?.developers ?? []).filter((d) => d.is_robot).length;
  const devRepos = Array.from(new Set(
    (devs.data?.developers ?? [])
      .filter((d) => developer.includes(d.identity))
      .flatMap((d) => d.repos),
  ));

  const list = useQuery({
    queryKey: ["browse", provider, page, query],
    queryFn: () => {
      const qs = new URLSearchParams({
        page: String(page), per_page: String(BROWSE_PAGE_SIZE),
      });
      if (query) qs.set("q", query);
      return api<RepoBrowseItem[]>(
        `/api/repos/browse/${provider}?${qs.toString()}`, { token },
      );
    },
    enabled: !!token,
    // Typing must not blank the list on every keystroke — the previous rows
    // stay put and get filtered client-side (below) until the answer arrives.
    placeholderData: keepPreviousData,
  });

  // Instant-feedback layer only: as long as the rows on screen are not yet the
  // provider's answer for what is typed, filter them locally. Once the answer
  // lands it wins outright — a client filter cannot know about repos it never
  // loaded, and a provider match need not be a substring of the full name
  // (GitLab searches the project name, Bitbucket the repo name).
  // A picked developer replaces the listing outright: the scan already knows
  // exactly which repos they touch, so there is nothing to search for.
  // A picked developer replaces the listing outright rather than filtering it.
  // The scan already knows every repo they touch, and those repos are spread
  // across the provider's pages — filtering the page on screen answered
  // "nothing found" for a repo that simply sits on page three.
  const rows: RepoBrowseItem[] = developer.length
    ? devRepos.map((full_name) => {
      const known = (list.data ?? []).find((r) => r.full_name === full_name);
      return known ?? {
        full_name,
        // Not in the loaded page, so the details are unknown. `provider:owner/name`
        // is what the add endpoint parses, so the row is still addable.
        url: `${provider}:${full_name}`,
        description: "",
        private: false,
        default_branch: "",
        already_added: false,
      };
    })
    : (list.data ?? []);
  const settled = typed === query && !list.isFetching;
  const visible = !settled && typed
    ? rows.filter((r) => r.full_name.toLowerCase().includes(typed.toLowerCase()))
    : rows;

  const add = useMutation({
    mutationFn: async (item: RepoBrowseItem) => {
      const body: RepoAddRequest = { url: item.url, auto_review: false };
      return api<RepoOut>("/api/repos", { method: "POST", token, json: body });
    },
    onSuccess: async (r, item) => {
      toast[indexInFlight(r) || r.index_status === "already_indexed"
        ? "success" : "info"](addedMessage(t, r, item.full_name));
      await onAdded();
      if (indexInFlight(r)) onIndexStart(r.slug);
      list.refetch();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-3 gap-2">
        {(["github", "gitlab", "bitbucket"] as const).map((p) => (
          <Button
            key={p}
            type="button"
            variant={provider === p ? "default" : "outline"}
            size="sm"
            onClick={() => {
              setProvider(p);
              setPage(1);
              // Logins do not carry across providers, and neither does the scan.
              setDeveloper([]);
              setDevScanOn(false);
            }}
          >
            {p}
          </Button>
        ))}
      </div>

      {/* Above the list, not below it: the list scrolls inside its own box, so
          a filter under it would be off-screen exactly when it is needed. */}
      <div className="relative">
        <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-muted-foreground)]" />
        <Input
          type="search"
          autoComplete="off"
          spellCheck={false}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="pl-8"
          placeholder={tf("repositories.browseSearchPlaceholder", "Filter by name — e.g. web-ui")}
          aria-label={tf("repositories.browseSearchLabel", "Search repositories by name")}
        />
      </div>
      {(owners.data?.length ?? 0) > 1 && (
        <Select
          value={ownerOf(search)}
          onChange={(v) => {
            // The owner IS the search: the browse endpoint matches the full
            // name, so "acme/" narrows to that account and keeps whatever
            // repo name was already typed.
            setSearch(v ? `${v}/${bareName(search)}` : bareName(search));
            setPage(1);
          }}
          options={[
            { value: "", label: tf("repositories.browseAnyOwner", "Any owner") },
            ...(owners.data ?? []).map((o) => ({
              value: o.owner,
              label: o.owner,
              hint: String(o.repo_count),
            })),
          ]}
          className="w-full"
          placeholder={tf("repositories.browseOwnerPlaceholder", "Owner…")}
        />
      )}
      {!devScanOn ? (
        <Button
          type="button" size="sm" variant="outline"
          onClick={() => setDevScanOn(true)}
        >
          <UsersIcon className="mr-1 h-3.5 w-3.5" />
          {tf("repositories.browseFindDevelopers", "Find developers")}
        </Button>
      ) : devs.isLoading ? (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {tf("repositories.browseDevelopersScanning", "Reading contributors from {provider}…", { provider })}
        </p>
      ) : devs.isError ? (
        <p className="text-xs text-[var(--color-destructive)]">
          {(devs.error as Error).message}
        </p>
      ) : (
        <div className="space-y-1">
          <div className="flex flex-wrap gap-1.5">
            {(devs.data?.developers ?? [])
              .filter((d) => showBots || !d.is_robot)
              .map((d) => {
              const on = developer.includes(d.identity);
              return (
                <Button
                  key={d.identity}
                  type="button"
                  size="sm"
                  variant={on ? "default" : "outline"}
                  className="max-w-full"
                  title={d.aliases.length ? [d.identity, ...d.aliases].join("\n") : d.identity}
                  onClick={() =>
                    setDeveloper((cur) =>
                      on ? cur.filter((x) => x !== d.identity) : [...cur, d.identity],
                    )
                  }
                >
                  <span className="truncate">{d.display_name || d.identity}</span>
                  <span className="ml-1.5 shrink-0 opacity-60">{d.repo_count}</span>
                </Button>
              );
            })}
          </div>
          {/* The scan is bounded, so say what it covered — a short list from a
              partial read is otherwise indistinguishable from a short team. */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {tf("repositories.browseDevelopersScanned",
                "Read the first {scanned} of {total} repositories.",
                { scanned: devs.data?.scanned ?? 0, total: devs.data?.total ?? 0 })}
            </p>
            {botCount > 0 && (
              // Real commits, made by servers and CI. Reported, not dropped —
              // but they do not belong between two colleagues in a people list.
              <button
                type="button"
                className="text-xs underline opacity-70 hover:opacity-100"
                onClick={() => setShowBots((v) => !v)}
              >
                {tf(showBots
                  ? "repositories.browseHideBots"
                  : "repositories.browseShowBots",
                  showBots ? "Hide {count} machine accounts" : "Show {count} machine accounts",
                  { count: botCount })}
              </button>
            )}
          </div>
          {developer.length > 0 && (
            <p className="text-xs text-[var(--color-brand)]">
              {tf("repositories.browseDeveloperPicked",
                "Showing {repos} repositories from {people} developer(s) — every page, not just this one.",
                { repos: devRepos.length, people: developer.length })}
            </p>
          )}
        </div>
      )}
      {query && (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {tf(
            "repositories.browseSearchScope",
            "Searching every page of {provider}, not just the one below.",
            { provider },
          )}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-[var(--color-muted-foreground)]">{t("repositories.pageLabel", { page })}</span>
        <div className="flex gap-1">
          <Button
            variant="ghost"
            size="sm"
            disabled={page <= 1 || list.isFetching}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            {t("repositories.prev")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={list.isFetching || rows.length < BROWSE_PAGE_SIZE}
            onClick={() => setPage((p) => p + 1)}
          >
            {t("repositories.next")}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            onClick={() => list.refetch()}
            disabled={list.isFetching}
          >
            <RefreshCwIcon className="h-3 w-3" />
          </Button>
        </div>
      </div>

      {list.isLoading ? (
        <div className="text-sm text-[var(--color-muted-foreground)] py-6 text-center">{t("repositories.loading")}</div>
      ) : list.error ? (
        <div className="text-sm text-[var(--color-destructive)] py-6 text-center">
          {(list.error as Error).message}
          <p className="text-[var(--color-muted-foreground)] mt-2">
            {t("repositories.connectHint", { provider })}
          </p>
        </div>
      ) : (
        <ul className="flex flex-col gap-1 max-h-[420px] overflow-y-auto">
          {visible.map((item) => (
            <li
              key={item.full_name}
              className="flex items-center justify-between gap-2 rounded p-2 hover:bg-[var(--color-accent)]"
            >
              <div className="min-w-0">
                <div className="text-sm font-medium truncate">{item.full_name}</div>
                {item.description && (
                  <div className="text-xs text-[var(--color-muted-foreground)] truncate">
                    {item.description}
                  </div>
                )}
              </div>
              {item.already_added ? (
                <Badge variant="outline">{t("repositories.addedBadge")}</Badge>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={add.isPending}
                  onClick={() => add.mutate(item)}
                >
                  <PlusIcon className="h-3 w-3" />
                  {t("repositories.addButton")}
                </Button>
              )}
            </li>
          ))}
          {visible.length === 0 && (
            <li className="text-center text-sm text-[var(--color-muted-foreground)] py-6">
              {typed
                ? tf("repositories.browseSearchEmpty",
                  "No repository on {provider} matches “{query}”.",
                  { provider, query: typed })
                : t("repositories.noReposFound")}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

function ProviderBadge({ provider }: { provider: Provider }) {
  const map: Record<Provider, { color: string; label: string }> = {
    github: { color: "bg-zinc-900 text-white", label: "GH" },
    gitlab: { color: "bg-orange-600 text-white", label: "GL" },
    bitbucket: { color: "bg-blue-600 text-white", label: "BB" },
  };
  const m = map[provider] || map.github;
  return (
    <div
      className={`flex h-7 w-7 items-center justify-center rounded text-xs font-bold ${m.color}`}
      title={provider}
    >
      {m.label}
    </div>
  );
}
