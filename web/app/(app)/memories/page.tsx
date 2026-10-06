"use client";

/**
 * /memories — what the team has taught the reviewers.
 *
 * A memory is one short fact every review is told ("we never log card
 * numbers", "this service must stay idempotent"): for the whole workspace,
 * for one repository, or for the files under one directory of it. People add
 * them here or with a command in a pull request. A memory a trusted person
 * asked for is active at once; one from anybody else, or one that changes an
 * existing memory's meaning, waits here PENDING until an editor approves it —
 * only active memories reach a prompt.
 *
 * `?repo=<slug>` opens a repository's memories directly (the review settings
 * link here from their Learning section).
 */

import { useMemo, useState, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import {
  BrainIcon, CheckIcon, ExternalLinkIcon, PencilIcon, PlusIcon, SearchIcon, ShieldOffIcon, Trash2Icon, XIcon,
} from "lucide-react";

import {
  api,
  memoriesApi,
  type Memory,
  type MemoryStatus,
  type RepoOut,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useCanEditPrompts } from "@/lib/use-analytics-access";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { LearningPanel } from "@/components/learning-panel";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Checkbox } from "@/components/ui/checkbox";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select } from "@/components/ui/select";
import { SkeletonRows } from "@/components/ui/skeleton";
import { OriginTag as OriginPill } from "@/components/ui/status";
import { Textarea } from "@/components/ui/textarea";

type Filter = "all" | MemoryStatus;
const FILTERS: Filter[] = ["active", "pending", "rejected", "all"];

/** Server limit (src/review/memories.py), repeated so a value is refused at
 *  the keyboard rather than by a 422. */
const MAX_TEXT = 800;

/** `?repo=` — the repository whose memories to open, or null. */
function repoFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("repo") || null;
}
const noSubscribe = () => () => {};
function viewFromUrl(): "memories" | "learning" {
  if (typeof window === "undefined") return "memories";
  return new URLSearchParams(window.location.search).get("view") === "learning"
    ? "learning" : "memories";
}

export default function MemoriesPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const { confirm, dialog } = useConfirm();

  // Owner / admin / editor only (`require_memories_access`): a viewer or member
  // is told so instead of being shown a page whose every request is a 403.
  const allowed = useCanEditPrompts();

  const urlRepo = useSyncExternalStore(noSubscribe, repoFromUrl, () => null);
  // undefined = follow the URL; null = the workspace scope; a slug = that repo.
  const [chosenRepo, setChosenRepo] = useState<string | null | undefined>(undefined);
  const repo = chosenRepo === undefined ? urlRepo : chosenRepo;
  const [repoQuery, setRepoQuery] = useState("");
  const urlView = useSyncExternalStore(noSubscribe, viewFromUrl, () => "memories" as const);
  const [chosenView, setChosenView] = useState<"memories" | "learning" | undefined>(undefined);
  const view = chosenView ?? urlView;
  const [filter, setFilter] = useState<Filter>("active");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [editing, setEditing] = useState<number | "new" | null>(null);

  const chooseRepo = (slug: string | null) => {
    setChosenRepo(slug);
    setSelected(new Set());
    setEditing(null);
    const url = new URL(window.location.href);
    if (slug) url.searchParams.set("repo", slug);
    else url.searchParams.delete("repo");
    window.history.replaceState(window.history.state, "", url.toString());
  };

  const chooseView = (v: "memories" | "learning") => {
    setChosenView(v);
    const url = new URL(window.location.href);
    if (v === "learning") url.searchParams.set("view", "learning");
    else url.searchParams.delete("view");
    window.history.replaceState(window.history.state, "", url.toString());
  };

  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token && allowed === true,
  });
  const repoSlugs = useMemo(
    () => (repos.data ?? []).map((r) => r.slug).sort((a, b) => a.localeCompare(b)),
    [repos.data],
  );

  const list = useQuery({
    queryKey: ["memories", repo ?? "", filter],
    queryFn: () => memoriesApi.list(token!, {
      scope: repo ? "all" : "workspace",
      repo,
      status: filter === "all" ? null : filter,
    }),
    enabled: !!token && allowed === true,
  });
  const preview = useQuery({
    queryKey: ["memories-preview", repo ?? ""],
    queryFn: () => memoriesApi.preview(token!, repo),
    enabled: !!token && allowed === true,
  });
  const canEdit = !!list.data?.can_edit;

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const rows = list.data?.memories ?? [];
    if (!needle) return rows;
    return rows.filter((r) => `${r.text} ${r.path_glob}`.toLowerCase().includes(needle));
  }, [list.data, search]);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["memories"] });
    qc.invalidateQueries({ queryKey: ["memories-preview"] });
  };

  const bulk = useMutation({
    mutationFn: async (action: "active" | "rejected" | "delete") => {
      const ids = Array.from(selected);
      if (action === "delete") return memoriesApi.bulkDelete(token!, ids);
      return memoriesApi.bulkStatus(token!, ids, action);
    },
    onSuccess: (_r, action) => {
      setSelected(new Set());
      refresh();
      toast.success(t(`memories.bulkDone.${action}`));
    },
    onError: (e) => toast.error(t("memories.saveFailed", { message: (e as Error).message })),
  });

  const one = useMutation({
    mutationFn: (v: { id: number; status: MemoryStatus }) =>
      memoriesApi.update(token!, v.id, { status: v.status }),
    onSuccess: () => refresh(),
    onError: (e) => toast.error(t("memories.saveFailed", { message: (e as Error).message })),
  });

  const removeSelected = async () => {
    const ok = await confirm({
      title: t("memories.confirmDelete", { count: selected.size }),
      confirmLabel: t("common.delete"),
      danger: true,
    });
    if (ok) bulk.mutate("delete");
  };

  const toggle = (id: number) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
  const allSelected = visible.length > 0 && visible.every((r) => selected.has(r.id));
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(visible.map((r) => r.id)));

  const counts = list.data?.counts;
  const budget = preview.data;
  const full = budget ? budget.chars / Math.max(1, budget.budget) : 0;

  if (allowed !== true) {
    return (
      <PageShell width="wide">
        <PageHeader
          icon={<BrainIcon className="h-6 w-6" />}
          title={t("memories.title")}
          description={t("memories.description")}
          tabs={<SectionTabs set="review" />}
        />
        {allowed === false && (
          <Card>
            <CardContent>
              <EmptyState
                icon={ShieldOffIcon}
                title={t("memories.forbiddenTitle")}
                description={t("memories.forbiddenDesc")}
              />
            </CardContent>
          </Card>
        )}
      </PageShell>
    );
  }

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<BrainIcon className="h-6 w-6" />}
        title={t("memories.title")}
        description={t("memories.description")}
        tabs={<SectionTabs set="review" />}
      />

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">{t("memories.scopeTitle")}</CardTitle>
          <CardDescription>{t("memories.scopeDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="sm:w-56">
            <Label htmlFor="mem-scope">{t("memories.scopeLabel")}</Label>
            <Select
              id="mem-scope"
              className="w-full"
              value={repo ? "repo" : "workspace"}
              onChange={(v) => {
                if (v === "workspace") chooseRepo(null);
                else if (repoSlugs.length) chooseRepo(repo ?? repoSlugs[0]);
              }}
              options={[
                { value: "workspace", label: t("memories.scopeWorkspace") },
                { value: "repo", label: t("memories.scopeRepo"), disabled: repoSlugs.length === 0 },
              ]}
            />
          </div>
          {repo && (
            <div className="flex-1">
              <Label htmlFor="mem-repo">{t("memories.repoLabel")}</Label>
              <Input
                id="mem-repo"
                list="mem-repos"
                value={repoQuery || repo}
                placeholder={t("memories.repoSearch")}
                onChange={(e) => {
                  setRepoQuery(e.target.value);
                  if (repoSlugs.includes(e.target.value)) {
                    chooseRepo(e.target.value);
                    setRepoQuery("");
                  }
                }}
              />
              <datalist id="mem-repos">
                {repoSlugs.map((s) => <option key={s} value={s} />)}
              </datalist>
            </div>
          )}
        </CardContent>
      </Card>

      <SegmentedControl
        semantics="tabs"
        size="sm"
        label={t("memories.view.label")}
        value={view}
        onValueChange={chooseView}
        segments={[
          { value: "memories" as const, label: t("memories.view.memories") },
          { value: "learning" as const, label: t("memories.view.learning") },
        ]}
      />

      {view === "learning" && <LearningPanel repo={repo} canEdit={canEdit} />}

      {view === "memories" && budget && (
        <Card>
          <CardContent className="space-y-2 pt-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
              <span className="font-medium">{t("memories.budgetTitle")}</span>
              <span className="tabular-nums text-[var(--color-muted-foreground)]">
                {t("memories.budgetChars", { used: budget.chars, budget: budget.budget })}
              </span>
            </div>
            <div
              role="meter"
              aria-label={t("memories.budgetTitle")}
              aria-valuemin={0}
              aria-valuemax={budget.budget}
              aria-valuenow={budget.chars}
              className="h-2 overflow-hidden rounded-full bg-[var(--color-neutral-soft)]"
            >
              <div
                className={full > 0.9
                  ? "h-full bg-[var(--color-warning)]"
                  : "h-full bg-[var(--color-primary)]"}
                style={{ width: `${Math.min(100, Math.round(full * 100))}%` }}
              />
            </div>
            {!budget.enabled && <Callout tone="warning">{t("memories.disabled")}</Callout>}
            {budget.omitted > 0 && (
              <Callout tone="warning">{t("memories.budgetOmitted", { count: budget.omitted })}</Callout>
            )}
          </CardContent>
        </Card>
      )}

      {view === "memories" && canEdit && (
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => setEditing("new")} disabled={editing === "new"}>
            <PlusIcon /> {t("memories.add")}
          </Button>
        </div>
      )}

      {view === "memories" && editing === "new" && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">{t("memories.newTitle")}</CardTitle>
            <CardDescription>
              {repo ? t("memories.newRepo", { repo }) : t("memories.newWorkspace")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <MemoryEditor
              hasRepo={!!repo}
              onCancel={() => setEditing(null)}
              onSave={async ({ text, path_glob }) => {
                await memoriesApi.create(token!, {
                  text, path_glob: path_glob || null, repo_slug: repo, status: "active",
                });
                setEditing(null);
                refresh();
                toast.success(t("memories.created"));
              }}
            />
          </CardContent>
        </Card>
      )}

      {view === "memories" && <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <SegmentedControl
              semantics="tabs"
              size="sm"
              label={t("memories.title")}
              value={filter}
              onValueChange={(f) => { setFilter(f); setSelected(new Set()); }}
              segments={FILTERS.map((f) => ({
                value: f,
                label: t(`memories.filter.${f}`),
                count: counts && (f === "pending" || f === "all") ? counts[f] : undefined,
              }))}
            />
            <div className="relative sm:w-64">
              <SearchIcon aria-hidden className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-subtle-foreground)]" />
              <Input
                className="pl-9"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("memories.searchPlaceholder")}
                aria-label={t("memories.searchPlaceholder")}
              />
            </div>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <AnimatePresence>
            {canEdit && selected.size > 0 && (
              <m.div
                key="bulk"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: 8, transition: { duration: 0.14 } }}
                transition={{ duration: 0.22, ease: [0.23, 1, 0.32, 1] }}
                className="sticky bottom-4 z-20 order-last"
              >
                <div className="clear-agent-launcher flex flex-wrap items-center gap-2 rounded-xl border border-[var(--color-border-strong)] bg-[var(--color-popover)] p-2 pl-3 text-sm shadow-[var(--shadow-lg)] sm:pr-2">
                  <span className="mr-auto font-medium tabular-nums">
                    {t("memories.selected", { count: selected.size })}
                  </span>
                  <Button size="sm" variant="secondary" onClick={() => bulk.mutate("active")}
                          disabled={bulk.isPending} loading={bulk.isPending && bulk.variables === "active"}>
                    <CheckIcon /> {t("memories.approve")}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => bulk.mutate("rejected")}
                          disabled={bulk.isPending} loading={bulk.isPending && bulk.variables === "rejected"}>
                    <XIcon /> {t("memories.reject")}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={removeSelected} disabled={bulk.isPending}
                          className="text-[var(--color-destructive)] hover:bg-[var(--color-destructive-soft)] hover:text-[var(--color-destructive)]">
                    <Trash2Icon /> {t("common.delete")}
                  </Button>
                </div>
              </m.div>
            )}
          </AnimatePresence>

          {filter === "pending" && (counts?.pending ?? 0) > 0 && (
            <Callout tone="info">{t("memories.whyPending")}</Callout>
          )}

          {list.isLoading && <SkeletonRows rows={4} />}
          {list.error && (
            <Callout tone="danger">{(list.error as Error).message}</Callout>
          )}
          {list.data && visible.length === 0 && (
            <EmptyState
              icon={BrainIcon}
              title={t("memories.emptyTitle")}
              description={t("memories.emptyDesc")}
            />
          )}

          {visible.length > 0 && canEdit && (
            <label className="flex w-fit cursor-pointer items-center gap-2.5 px-3 py-1 text-xs text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]">
              <Checkbox checked={allSelected} onChange={toggleAll}
                        aria-label={t("memories.selectAll")} />
              {t("memories.selectAll")}
            </label>
          )}

          {visible.map((memory) => (
            <MemoryRow
              key={memory.id}
              memory={memory}
              canEdit={canEdit}
              checked={selected.has(memory.id)}
              onCheck={() => toggle(memory.id)}
              editing={editing === memory.id}
              onEdit={() => setEditing(editing === memory.id ? null : memory.id)}
              onSetStatus={(status) => one.mutate({ id: memory.id, status })}
              onSave={async ({ text, path_glob }) => {
                await memoriesApi.update(token!, memory.id, { text, path_glob });
                setEditing(null);
                refresh();
                toast.success(t("memories.saved"));
              }}
            />
          ))}
        </CardContent>
      </Card>}
      {dialog}
    </PageShell>
  );
}

function MemoryRow({
  memory, canEdit, checked, onCheck, editing, onEdit, onSetStatus, onSave,
}: {
  memory: Memory;
  canEdit: boolean;
  checked: boolean;
  onCheck: () => void;
  editing: boolean;
  onEdit: () => void;
  onSetStatus: (s: MemoryStatus) => void;
  onSave: (v: { text: string; path_glob: string }) => Promise<void>;
}) {
  const t = useT();
  return (
    <div className="rounded-lg border border-[var(--color-border)]">
      <div className="flex items-start gap-3 p-3">
        {canEdit && (
          <Checkbox className="mt-0.5" checked={checked} onChange={onCheck}
                    aria-label={t("memories.selectMemory")} />
        )}
        <div className="min-w-0 flex-1 space-y-1.5">
          <p className="whitespace-pre-wrap break-words text-sm">{memory.text}</p>
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <Badge variant="outline">{t(`memories.scope.${memory.scope}`)}</Badge>
            {memory.path_glob && (
              <code className="rounded bg-[var(--color-neutral-soft)] px-1.5 py-0.5 font-mono text-[11px]">
                {memory.path_glob}
              </code>
            )}
            {memory.repo_slug && (
              <span className="text-[var(--color-muted-foreground)]">{memory.repo_slug}</span>
            )}
            <OriginPill origin={memory.origin} label={t(`memories.origin.${memory.origin}`)} />
            {memory.status !== "active" && (
              <Badge variant={memory.status === "pending" ? "attention" : "default"}>
                {t(`memories.status.${memory.status}`)}
              </Badge>
            )}
            {memory.created_by && (
              <span className="text-[var(--color-muted-foreground)]">
                {t("memories.by", { who: memory.created_by })}
              </span>
            )}
            {memory.source_url && (
              <a
                href={memory.source_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 text-[var(--color-primary)] underline-offset-4 hover:underline"
              >
                {t("memories.source")} <ExternalLinkIcon aria-hidden className="size-3" />
              </a>
            )}
          </div>
        </div>
        {canEdit && (
          <div className="flex shrink-0 gap-1">
            {memory.status !== "active" && (
              <Button size="sm" variant="secondary" onClick={() => onSetStatus("active")}>
                <CheckIcon /> {t("memories.approve")}
              </Button>
            )}
            {memory.status === "pending" && (
              <Button size="sm" variant="outline" onClick={() => onSetStatus("rejected")}>
                <XIcon /> {t("memories.reject")}
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={onEdit} aria-label={t("memories.edit")}
                    aria-expanded={editing}>
              <PencilIcon />
            </Button>
          </div>
        )}
      </div>
      {editing && (
        <div className="border-t border-[var(--color-border)] p-3">
          <MemoryEditor
            hasRepo={!!memory.repo_slug}
            initial={{ text: memory.text, path_glob: memory.path_glob }}
            onCancel={onEdit}
            onSave={onSave}
          />
        </div>
      )}
    </div>
  );
}

function MemoryEditor({
  hasRepo, initial, onSave, onCancel,
}: {
  hasRepo: boolean;
  initial?: { text: string; path_glob: string };
  onSave: (v: { text: string; path_glob: string }) => Promise<void>;
  onCancel: () => void;
}) {
  const t = useT();
  const [text, setText] = useState(initial?.text ?? "");
  const [glob, setGlob] = useState(initial?.path_glob ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const valid = text.trim().length > 0 && text.length <= MAX_TEXT;
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSave({ text: text.trim(), path_glob: hasRepo ? glob.trim() : "" });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-3">
      <div>
        <Label htmlFor="mem-text">{t("memories.labelText")}</Label>
        <Textarea
          id="mem-text"
          rows={3}
          value={text}
          maxLength={MAX_TEXT}
          placeholder={t("memories.placeholderText")}
          onChange={(e) => setText(e.target.value)}
        />
        <p className="mt-1 text-right text-xs tabular-nums text-[var(--color-muted-foreground)]">
          {text.length} / {MAX_TEXT}
        </p>
      </div>
      {hasRepo && (
        <div>
          <Label htmlFor="mem-glob">{t("memories.labelGlob")}</Label>
          <Input
            id="mem-glob"
            className="font-mono text-xs"
            value={glob}
            placeholder="src/billing"
            onChange={(e) => setGlob(e.target.value)}
          />
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">
            {t("memories.globHint")}
          </p>
        </div>
      )}
      {error && <Callout tone="danger">{error}</Callout>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onCancel} disabled={busy}>{t("common.cancel")}</Button>
        <Button onClick={submit} disabled={!valid} loading={busy}>{t("common.save")}</Button>
      </div>
    </div>
  );
}
