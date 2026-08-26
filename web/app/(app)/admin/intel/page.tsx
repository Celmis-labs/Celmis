"use client";

/**
 * /admin/intel — repo intelligence read-views (parity).
 *
 * Surfaces the previously-UI-less intel endpoints: architecture summary,
 * ownership snapshot and reverse-index, each with a rebuild action.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import Link from "next/link";
import { FolderGit2Icon, NetworkIcon, RefreshCwIcon } from "lucide-react";

import { api, intelApi, type RepoOut, type ReverseIndexOut } from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import {
  Card, CardContent, CardHeader, CardTitle,
} from "@/components/ui/card";
import { HelpButton } from "@/components/ui/help-button";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Label } from "@/components/ui/label";
import { QueryState } from "@/components/ui/query-state";
import { Select } from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";

type Tab = "architecture" | "ownership" | "reverse";

export default function IntelPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [slug, setSlug] = useState("");
  const [tab, setTab] = useState<Tab>("architecture");
  const [helpOpen, setHelpOpen] = useState(false);
  const [lookback, setLookback] = useState("90");

  // The picker used to list /api/qa/available-repos, which only returns repos
  // that already have vault points in Qdrant — so it was empty for everyone who
  // had added and indexed repos but never generated a vault. Intel views only
  // need a registered repo, so list those.
  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });
  const repoList = repos.data ?? [];
  const selected = repoList.find((r) => r.slug === slug);

  // Auto-pick so the page is never a dead end: prefer an indexed repo, since
  // un-indexed ones have nothing to show.
  useEffect(() => {
    if (slug || repoList.length === 0) return;
    setSlug((repoList.find((r) => r.indexed) ?? repoList[0]).slug);
  }, [slug, repoList]);

  const arch = useQuery({
    queryKey: ["intel", "arch", slug],
    queryFn: () => intelApi.architecture(token!, slug),
    enabled: !!token && !!slug && tab === "architecture",
  });
  const own = useQuery({
    queryKey: ["intel", "own", slug],
    queryFn: () => intelApi.ownership(token!, slug),
    enabled: !!token && !!slug && tab === "ownership",
  });
  const rev = useQuery({
    queryKey: ["intel", "rev", slug],
    queryFn: () => intelApi.reverseIndex(token!, slug),
    enabled: !!token && !!slug && tab === "reverse",
  });

  const rebuildArch = useMutation({
    mutationFn: () => intelApi.rebuildArchitecture(token!, slug),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["intel", "arch", slug] });
      toast.success(t("admin.intel.archRebuilt"));
    },
    onError: (e) => toast.error((e as Error).message),
  });
  const rebuildOwn = useMutation({
    mutationFn: () => intelApi.rebuildOwnership(token!, slug, Number(lookback)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["intel", "own", slug] });
      toast.success(t("admin.intel.ownRebuilt"));
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<NetworkIcon className="h-6 w-6" />}
        title={t("admin.intel.heading")}
        badge={
          <HelpButton onClick={() => setHelpOpen(true)} aria-label={t("admin.intel.helpTitle")} />
        }
        description={t("admin.intel.description")}
        tabs={<SectionTabs set="sources" />}
      />

      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("admin.intel.helpTitle")}</DialogTitle>
            <DialogDescription>{t("admin.intel.helpIntro")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.intel.helpArchitecture")}</p>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.intel.helpOwnership")}</p>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.intel.helpReverse")}</p>
            <p className="text-xs text-[var(--color-muted-foreground)]">{t("admin.intel.helpRebuild")}</p>
          </div>
        </DialogContent>
      </Dialog>

      <Card>
        <CardContent className="pt-6">
          <QueryState
            query={repos}
            skeleton={1}
            empty={{
              icon: FolderGit2Icon,
              title: t("admin.intel.noRepos"),
              description: t("admin.intel.noReposDescription"),
              action: (
                <Link
                  href="/repositories"
                  className={buttonVariants({ variant: "outline", size: "sm" })}
                >
                  {t("admin.intel.addRepoCta")}
                </Link>
              ),
            }}
          >
            {(list) => (
              <div className="grid gap-3 sm:grid-cols-[1fr_auto] items-end">
                <div>
                  <Label>{t("admin.intel.repoLabel")}</Label>
                  <Select
                    className="w-full"
                    value={slug} onChange={(v) => setSlug(v)}
                    placeholder={t("admin.intel.selectPlaceholder")}
                    options={list.map((r) => ({
                      value: r.slug,
                      label: r.indexed
                        ? r.full_name
                        : `${r.full_name} · ${t("admin.intel.notIndexedSuffix")}`,
                    }))}
                  />
                </div>
                <div className="flex gap-1">
                  {(["architecture", "ownership", "reverse"] as Tab[]).map((tabId) => (
                    <button
                      key={tabId}
                      onClick={() => setTab(tabId)}
                      className={`rounded-md px-3 py-1.5 text-xs ${
                        tab === tabId ? "bg-[var(--color-brand-muted)] text-[var(--color-brand)]" : "hover:bg-[var(--color-accent)]"
                      }`}
                    >
                      {t(`admin.intel.tab.${tabId}`)}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </QueryState>
        </CardContent>
      </Card>

      {repos.isSuccess && repoList.length > 0 && !slug && (
        <p className="text-sm text-[var(--color-muted-foreground)]">{t("admin.intel.selectRepo")}</p>
      )}

      {selected && !selected.indexed && (
        <Callout tone="warning">
          {t("admin.intel.notIndexedWarning", { name: selected.full_name })}{" "}
          <Link href="/repositories" className="underline underline-offset-2">
            {t("admin.intel.addRepoCta")}
          </Link>
        </Callout>
      )}

      {slug && tab === "architecture" && (
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle className="text-base">{t("admin.intel.architectureTitle")}</CardTitle>
            <Button size="sm" variant="outline" onClick={() => rebuildArch.mutate()} disabled={rebuildArch.isPending}>
              <RefreshCwIcon className="h-3.5 w-3.5 mr-1" /> {t("admin.intel.rebuild")}
            </Button>
          </CardHeader>
          <CardContent className="space-y-2">
            {arch.isLoading ? t("admin.intel.loading") : (
              <>
                {arch.data?.computed_at && (
                  <p className="text-xs text-[var(--color-muted-foreground)]">
                    {t("admin.intel.computedAt", { time: formatDateTime(arch.data.computed_at) })}
                    {arch.data.model_used ? ` · ${arch.data.model_used}` : ""}
                  </p>
                )}
                <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap rounded bg-[var(--color-muted)] p-3 text-xs">
                  {arch.data?.summary_md || t("admin.intel.noSummary")}
                </pre>
              </>
            )}
          </CardContent>
        </Card>
      )}

      {slug && tab === "ownership" && (
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle className="text-base">{t("admin.intel.ownershipTitle")}</CardTitle>
            <div className="flex items-center gap-2">
              <Select
                className="h-11 sm:h-8 w-20 text-base sm:text-xs"
                value={lookback}
                onChange={(v) => setLookback(v)}
                options={["30", "90", "180", "365"].map((d) => ({ value: d, label: d }))}
              />
              <Button size="sm" variant="outline" onClick={() => rebuildOwn.mutate()} disabled={rebuildOwn.isPending}>
                <RefreshCwIcon className="h-3.5 w-3.5 mr-1" /> {t("admin.intel.rebuild")}
              </Button>
            </div>
          </CardHeader>
          <CardContent className="text-sm space-y-1">
            {own.isLoading ? t("admin.intel.loading") : (
              <>
                {own.data?.computed_at ? (
                  <p className="text-xs text-[var(--color-muted-foreground)]">
                    {t("admin.intel.computedAt", { time: formatDateTime(own.data.computed_at) })}
                    {" · "}
                    {t("admin.intel.lookbackLabel", { days: String(own.data.lookback_days) })}
                  </p>
                ) : own.data ? (
                  <p className="text-xs text-[var(--color-muted-foreground)]">
                    {t("admin.intel.neverComputed")}
                  </p>
                ) : null}
                <div>{t("admin.intel.filesAuthors", { files: own.data?.stats?.files_total ?? "—", authors: own.data?.stats?.distinct_authors ?? "—" })}</div>
                <ul className="list-disc pl-5">
                  {(own.data?.stats?.top_owners ?? []).map((o) => (
                    <li key={o.identity}>{t("admin.intel.ownerLine", { identity: o.identity, commits: o.commits })}</li>
                  ))}
                </ul>
              </>
            )}
          </CardContent>
        </Card>
      )}

      {slug && tab === "reverse" && (
        <Card>
          <CardHeader><CardTitle className="text-base">{t("admin.intel.reverseTitle")}</CardTitle></CardHeader>
          <CardContent>
            {rev.isLoading ? t("admin.intel.loading") : <ReverseIndexView data={rev.data} />}
          </CardContent>
        </Card>
      )}
    </PageShell>
  );
}

/** Structured {source_file → [note_paths]} view with a raw-JSON fallback for
 * any payload that does not match the expected shape. */
function ReverseIndexView({ data }: { data: ReverseIndexOut | undefined }) {
  const t = useT();
  const idx = data?.index;
  const structured =
    !!idx && typeof idx === "object" && !Array.isArray(idx) &&
    Object.values(idx).every(
      (v) => Array.isArray(v) && v.every((n) => typeof n === "string"),
    );

  if (!structured) {
    return (
      <pre className="max-h-[28rem] overflow-auto rounded bg-[var(--color-muted)] p-3 text-[11px] font-mono">
        {JSON.stringify(data ?? {}, null, 2)}
      </pre>
    );
  }

  const entries = Object.entries(idx as Record<string, string[]>)
    .sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) {
    return (
      <p className="text-sm text-[var(--color-muted-foreground)]">
        {t("admin.intel.revEmpty")}
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("admin.intel.revCounts", {
          files: data?.source_file_count ?? entries.length,
          notes: data?.note_count ?? 0,
        })}
      </p>
      <div className="max-h-[28rem] space-y-1.5 overflow-auto">
        {entries.map(([file, notes]) => (
          <div key={file} className="rounded border border-[var(--color-border)] p-2">
            <code className="text-xs font-medium">{file}</code>
            <div className="mt-1 flex flex-wrap gap-1">
              {notes.map((n) => (
                <Badge key={n} variant="outline" className="font-mono text-[9px]">
                  {n}
                </Badge>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
