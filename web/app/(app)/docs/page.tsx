"use client";

/**
 * /docs — the documentation the generator was already writing.
 *
 * "Generate vault" produced one markdown note per module and then showed none
 * of it: the notes went into the search index and nowhere else. So the feature
 * read as a prerequisite for Q&A — which it never was. Q&A answers from source
 * with code display on, vault or no vault. This is the output, readable and
 * exportable.
 *
 * PDF is the browser's, not the server's. The print stylesheet drops the app
 * chrome and prints this page, which means the PDF has the same typography the
 * reader just approved on screen — and no cairo/pango in the image.
 */

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  BookTextIcon, DownloadIcon, FileTextIcon, FileUpIcon, PrinterIcon, RefreshCwIcon,
  SparklesIcon, WorkflowIcon,
} from "lucide-react";

import {
  api, API_BASE, docsApi, downloadWithAuth, kbApi, requestHeaders, type KbFilters, type RepoOut,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Markdown } from "@/components/markdown";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { Select } from "@/components/ui/select";
import { WorkspaceBadge } from "@/components/workspace-badge";
import {
  filtersActive, KbCardPanel, KbChangesView, KbFilterBar, KbGraphView, KbImportPanel, StatusBadge,
} from "@/components/docs/knowledge-base";
import { ProcessesPanel } from "@/components/docs/processes";

type NoteSummary = {
  path: string; title: string; updated_at?: string; words?: number;
  /** A generated note's kind (module, feature, …) or an imported card's own type. */
  type?: string; status?: string; card?: boolean;
  /** "generate": a process note Celmis wrote from the code. */
  origin?: string;
};
type View = "notes" | "links" | "changes";
type DocsOverview = { repo_slug: string; notes: NoteSummary[]; omitted: number };
type NoteOut = { path: string; title: string; body: string; updated_at: string };

export default function DocsPage() {
  const t = useT();
  const token = useToken();
  const [repo, setRepo] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [view, setView] = useState<View>("notes");
  const [showImport, setShowImport] = useState(false);
  const [showProcesses, setShowProcesses] = useState(false);
  const [filters, setFilters] = useState<KbFilters>({});

  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });
  const slug = repo || repos.data?.[0]?.slug || "";

  const docs = useQuery({
    queryKey: ["docs", slug],
    queryFn: () => api<DocsOverview>(`/api/docs/${slug}`, { token }),
    enabled: !!token && !!slug,
  });

  // The list narrowed by front matter (type, status, a 1C object, any key):
  // the knowledge-base route, which reads the same vault with the cards' fields.
  const filtering = filtersActive(filters);
  const kbList = useQuery({
    queryKey: ["kb", slug, filters],
    queryFn: () => kbApi.list(token!, slug, filters),
    enabled: !!token && !!slug,
  });

  const note = useQuery({
    queryKey: ["docs-note", slug, open],
    queryFn: () =>
      api<NoteOut>(`/api/docs/${slug}/note?path=${encodeURIComponent(open!)}`, { token }),
    enabled: !!token && !!slug && !!open,
  });

  const download = async (format: "md" | "docx") => {
    try {
      // Not a plain <a href>: the endpoint needs the bearer token, so the file
      // is fetched and handed to the browser as a blob.
      const resp = await fetch(`${API_BASE}/api/docs/${slug}/export?format=${format}`, {
        headers: requestHeaders(token),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${slug}.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const allNotes: NoteSummary[] = docs.data?.notes ?? [];
  const notes: NoteSummary[] = filtering ? (kbList.data?.notes ?? []) : allNotes;
  const types = kbList.data?.types ?? [];
  const openNote = (path: string) => { setOpen(path); setView("notes"); };
  // A note Celmis generated from the code (a process note) is a card in shape only:
  // it keeps the regenerate controls an imported card does not have.
  const openSummary = notes.find((n) => n.path === open);
  const openIsCard = !!open && openSummary?.origin !== "generate"
    && (open.startsWith("kb/") || !!openSummary?.card);

  // Redo THIS document, optionally with the other engine.
  //
  // A vault build is dozens of documents and many minutes, so without this the
  // only way to improve one weak PRD — to have the agent research it properly
  // instead of the API model summarising the code it was handed — was to run
  // the whole thing again.
  const [regenEngine, setRegenEngine] = useState<string>("claude_code");
  // "Generate for everything that has none" — the request a form answers
  // badly, because the set is defined by a condition rather than enumerated.
  const generateMissing = useMutation({
    mutationFn: () => docsApi.generateBulk(token!, { missing_only: true }),
    onSuccess: (r) => {
      if (!r.queued.length) {
        // Every repository was skipped. Saying which and why beats a spinner
        // that stops and a list that does not change.
        toast.info(t("docs.generateNothing", {
          detail: r.skipped.slice(0, 3).map((s) => `${s.repo}: ${s.reason}`)
            .join("; ") || "—",
        }));
        return;
      }
      toast.success(t("docs.generateQueued", {
        count: String(r.queued.length),
        skipped: String(r.skipped.length),
      }));
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const downloadAll = async () => {
    try {
      await downloadWithAuth(docsApi.exportAllUrl(), "celmis-documentation.zip", token);
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const regenerate = useMutation({
    mutationFn: () =>
      docsApi.regenerate(token!, slug, {
        note_paths: [open!],
        engine: regenEngine,
      }),
    onSuccess: (r) => {
      // The endpoint answers 202 whether or not anything was enqueued —
      // `enqueue` returns None on a dedup hit. Reporting both as success made
      // pressing the button twice look like it worked twice.
      if (r.queued === false) toast.info(r.detail);
      else toast.success(r.detail);
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<BookTextIcon className="h-6 w-6" />}
        title={t("docs.title")}
        badge={<WorkspaceBadge />}
        description={t("docs.subtitle")}
        tabs={<SectionTabs set="sources" />}
      />

      <div className="flex flex-wrap items-center gap-2 print:hidden">
        <Select
          value={slug}
          onChange={(v) => { setRepo(v); setOpen(null); setFilters({}); }}
          options={(repos.data ?? []).map((r) => ({ value: r.slug, label: r.full_name }))}
          className="w-full sm:w-72"
          placeholder={t("docs.pickRepo")}
        />
        <Button size="sm" variant={showImport ? "default" : "outline"} disabled={!slug}
                onClick={() => setShowImport((v) => !v)} data-testid="kb-toggle-import">
          <FileUpIcon className="mr-1 h-3.5 w-3.5" /> {t("kb.importButton")}
        </Button>
        <Button size="sm" variant={showProcesses ? "default" : "outline"} disabled={!slug}
                onClick={() => setShowProcesses((v) => !v)} data-testid="proc-toggle">
          <WorkflowIcon className="mr-1 h-3.5 w-3.5" /> {t("proc.button")}
        </Button>
        <Button size="sm" variant="outline" disabled={!allNotes.length}
                onClick={() => void download("md")}>
          <DownloadIcon className="mr-1 h-3.5 w-3.5" /> {t("docs.exportMd")}
        </Button>
        <Button size="sm" variant="outline" disabled={!allNotes.length}
                onClick={() => void download("docx")}>
          <FileTextIcon className="mr-1 h-3.5 w-3.5" /> {t("docs.exportDocx")}
        </Button>
        <Button size="sm" variant="outline" disabled={!allNotes.length}
                onClick={() => window.print()}>
          <PrinterIcon className="mr-1 h-3.5 w-3.5" /> {t("docs.exportPdf")}
        </Button>

        {/* The two above act on the repository in the picker. These two act on
            the workspace — which is what somebody actually asks for when they
            say "the documentation": an auditor wants the set, and a platform
            of nine services meant nine downloads and nine filenames. */}
        <span className="mx-1 hidden h-5 w-px bg-[var(--color-border)] sm:inline-block" />
        <Button size="sm" variant="outline"
                disabled={generateMissing.isPending}
                onClick={() => generateMissing.mutate()}>
          <SparklesIcon className={cn("mr-1 h-3.5 w-3.5",
            generateMissing.isPending && "animate-spin")} />
          {t("docs.generateMissing")}
        </Button>
        <Button size="sm" variant="outline" onClick={() => void downloadAll()}>
          <DownloadIcon className="mr-1 h-3.5 w-3.5" /> {t("docs.exportAll")}
        </Button>
      </div>

      {showImport && slug && (
        <KbImportPanel token={token} slug={slug} repos={repos.data ?? []} />
      )}
      {showProcesses && slug && (
        <ProcessesPanel key={slug} token={token} slug={slug} onOpen={openNote} />
      )}

      {allNotes.length > 0 && (
        <SegmentedControl<View>
          value={view}
          onValueChange={setView}
          label={t("kb.views")}
          semantics="tabs"
          size="sm"
          className="print:hidden"
          segments={[
            { value: "notes", label: t("kb.viewNotes") },
            { value: "links", label: t("kb.viewLinks") },
            { value: "changes", label: t("kb.viewChanges") },
          ]}
        />
      )}

      {view === "links" && allNotes.length > 0 && (
        <KbGraphView token={token} slug={slug} onOpen={openNote} />
      )}
      {view === "changes" && allNotes.length > 0 && (
        <KbChangesView token={token} slug={slug} onOpen={openNote} />
      )}

      {slug && !docs.isLoading && allNotes.length === 0 && (
        <EmptyState
          icon={BookTextIcon}
          title={t("docs.emptyTitle")}
          description={t("docs.emptyBody")}
          action={
            <Button size="sm" variant="outline" onClick={() => docs.refetch()}>
              <RefreshCwIcon className="mr-1 h-3.5 w-3.5" /> {t("docs.recheck")}
            </Button>
          }
        />
      )}

      {view === "notes" && allNotes.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
          <Card className="print:hidden">
            <CardHeader className="space-y-3 pb-2">
              <CardTitle className="text-sm">
                {filtering
                  ? t("kb.matching", { count: notes.length, total: allNotes.length })
                  : t("docs.notesCount", { count: notes.length })}
              </CardTitle>
              <KbFilterBar value={filters} onChange={setFilters} types={types} />
            </CardHeader>
            <CardContent className="max-h-[60vh] space-y-0.5 overflow-y-auto">
              {filtering && notes.length === 0 && (
                <p className="px-2 py-1.5 text-xs text-[var(--color-muted-foreground)]">{t("kb.noMatch")}</p>
              )}
              {notes.map((n) => (
                <button
                  key={n.path}
                  type="button"
                  onClick={() => setOpen(n.path)}
                  className={`flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs hover:bg-[var(--color-accent)] ${
                    open === n.path ? "bg-[var(--color-accent)] font-medium" : ""
                  }`}
                  title={n.type ? `${n.path} · ${n.type}` : n.path}
                >
                  <span className="min-w-0 flex-1 truncate">{n.title}</span>
                  {n.card && <StatusBadge status={n.status} />}
                </button>
              ))}
              {!filtering && docs.data!.omitted > 0 && (
                // Never a silent cap: a partial list that looks complete is
                // how someone concludes a module was never documented.
                <p className="px-2 pt-2 text-xs text-[var(--color-muted-foreground)]">
                  {t("docs.omitted", { count: docs.data!.omitted })}
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="prose-sm max-w-none pt-4">
              {open && note.data ? (
                <>
                  <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
                    <h2 className="text-lg font-semibold">{note.data.title}</h2>
                    {/* Per-document, and the engine is picked here rather than
                        inherited: the reason to redo one PRD is usually that
                        this particular one came out thin, which is an argument
                        about the engine, not about the workspace. */}
                    {!openIsCard && <div className="flex items-center gap-2 print:hidden">
                      <Select
                        value={regenEngine}
                        onChange={setRegenEngine}
                        className="w-48"
                        options={[
                          { value: "claude_code", label: t("repositories.vaultEngineAgent") },
                          { value: "api", label: t("repositories.vaultEngineApi") },
                        ]}
                      />
                      <Button size="sm" variant="outline"
                              disabled={regenerate.isPending}
                              onClick={() => regenerate.mutate()}>
                        <RefreshCwIcon className={cn("mr-1 h-3.5 w-3.5",
                          regenerate.isPending && "animate-spin")} />
                        {t("docs.regenerate")}
                      </Button>
                    </div>}
                  </div>
                  <p className="mb-3 font-mono text-xs text-[var(--color-muted-foreground)]">
                    {note.data.path}
                  </p>
                  <KbCardPanel token={token} slug={slug} path={note.data.path} onOpen={openNote} />
                  <Markdown text={note.data.body} />
                </>
              ) : (
                <p className="text-sm text-[var(--color-muted-foreground)]">
                  {t("docs.pickNote")}
                </p>
              )}
            </CardContent>
          </Card>
        </div>
      )}
    </PageShell>
  );
}
