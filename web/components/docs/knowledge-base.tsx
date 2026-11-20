"use client";

/**
 * The team's own cards in a repository's documentation (`/api/kb`).
 *
 * A card is a markdown file with front matter — a process, a decision, a
 * glossary entry — imported next to the generated notes. What this file adds
 * to /docs: the import (files, a zip, or the repository's own markdown that is
 * imported again after every push), the status of every note (a draft is not
 * a checked fact, and a card whose 1C objects changed since its commit reads
 * "needs review"), the card's own panel (front matter, objects, links in and
 * out), a graph of the links, and the log of what changed.
 */

import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FileUpIcon, Loader2Icon, RefreshCwIcon, Trash2Icon } from "lucide-react";

import { kbApi, type KbCard, type KbFilters, type KbGraph, type KbReport, type RepoOut } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { filtersActive, GRAPH_H as H, GRAPH_W as W, layout } from "@/lib/kb-graph";
import { cn } from "@/lib/utils";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";

const ACCEPT = ".md,.markdown,.zip";
export const STATUSES = ["verified", "needs-review", "draft"] as const;

const STATUS_VARIANT: Record<string, BadgeVariant> = {
  verified: "success", "needs-review": "warning", draft: "outline",
};

/** The status of a card, or nothing for a generated note. */
export function StatusBadge({ status }: { status?: string }) {
  const t = useT();
  if (!status) return null;
  const known = (STATUSES as readonly string[]).includes(status);
  return (
    <Badge variant={STATUS_VARIANT[status] ?? "outline"} data-testid="kb-status">
      {known ? t(`kb.status.${status}`) : status}
    </Badge>
  );
}

// ─── import ──────────────────────────────────────────────────────────────

export function KbImportPanel({ token, slug, repos }: {
  token: string | null | undefined; slug: string; repos: RepoOut[];
}) {
  const t = useT();
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const { confirm, dialog } = useConfirm();
  const [dragging, setDragging] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [codeRepo, setCodeRepo] = useState<string | null>(null);
  const [replace, setReplace] = useState(false);
  const [prefix, setPrefix] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const [report, setReport] = useState<KbReport | null>(null);

  const state = useQuery({
    queryKey: ["kb", slug, {}],
    queryFn: () => kbApi.list(token!, slug),
    enabled: !!token && !!slug,
  });
  const binding = state.data?.binding ?? {};
  const code = codeRepo ?? binding.code_repo ?? "";
  const pre = prefix ?? binding.prefix ?? "";

  const done = (r: KbReport) => {
    setReport(r);
    setFiles([]);
    void qc.invalidateQueries({ queryKey: ["kb", slug] });
    void qc.invalidateQueries({ queryKey: ["docs", slug] });
    toast.success(t("kb.imported", {
      created: r.created.length, updated: r.updated.length, deleted: r.deleted.length,
    }));
  };
  const fail = (e: unknown) => toast.error((e as Error).message);

  const upload = useMutation({
    mutationFn: () => kbApi.upload(token!, slug, files, { code_repo: code, replace }),
    onSuccess: done, onError: fail,
  });
  const fromRepo = useMutation({
    mutationFn: () => kbApi.fromRepo(token!, slug, { prefix: pre, code_repo: code, follow }),
    onSuccess: done, onError: fail,
  });
  const check = useMutation({
    mutationFn: () => kbApi.check(token!, slug),
    onSuccess: (r) => { setReport(r); void qc.invalidateQueries({ queryKey: ["kb", slug] }); },
    onError: fail,
  });
  const remove = useMutation({
    mutationFn: () => kbApi.removeAll(token!, slug),
    onSuccess: (r) => {
      setReport(null);
      void qc.invalidateQueries({ queryKey: ["kb", slug] });
      void qc.invalidateQueries({ queryKey: ["docs", slug] });
      toast.success(t("kb.removed", { count: r.removed }));
    },
    onError: fail,
  });

  const pick = (list: FileList | null | undefined) => {
    if (list?.length) setFiles(Array.from(list));
  };
  const busy = upload.isPending || fromRepo.isPending;

  return (
    <Card className="print:hidden" data-testid="kb-import">
      {dialog}
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{t("kb.importTitle")}</CardTitle>
        <CardDescription>{t("kb.importHelp")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {binding.imported_at && (
          <p className="text-xs text-[var(--color-muted-foreground)]" data-testid="kb-binding">
            {binding.source === "repo"
              ? t("kb.boundRepo", { prefix: binding.prefix || "/", follow: binding.follow ? t("kb.followOn") : t("kb.followOff") })
              : t("kb.boundUpload")}
            {" · "}{t("kb.cardsCount", { count: binding.cards ?? 0 })}
            {binding.code_repo ? ` · ${t("kb.checkedAgainst", { repo: binding.code_repo })}` : ""}
            {" · "}{new Date(binding.imported_at).toLocaleString()}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1">
            <span className="text-xs font-medium">{t("kb.codeRepo")}</span>
            <Select
              value={code}
              onChange={setCodeRepo}
              options={[{ value: "", label: t("kb.codeRepoNone") },
                ...repos.map((r) => ({ value: r.slug, label: r.full_name }))]}
              className="w-full"
            />
            <span className="block text-xs text-[var(--color-muted-foreground)]">{t("kb.codeRepoHelp")}</span>
          </label>
        </div>

        <div
          role="button"
          tabIndex={0}
          data-testid="kb-drop"
          onClick={() => input.current?.click()}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") input.current?.click(); }}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); pick(e.dataTransfer.files); }}
          className={cn(
            "flex cursor-pointer flex-col items-center gap-1 rounded-lg border border-dashed px-4 py-5 text-center",
            "border-[var(--color-border-strong)] hover:bg-[var(--color-muted)]/40",
            dragging && "bg-[var(--color-muted)]/60",
          )}
        >
          {upload.isPending ? <Loader2Icon className="h-5 w-5 animate-spin" /> : <FileUpIcon className="h-5 w-5" />}
          <span>{files.length ? t("kb.filesPicked", { count: files.length }) : t("kb.drop")}</span>
          <input
            ref={input} type="file" multiple accept={ACCEPT} className="hidden" data-testid="kb-input"
            onChange={(e) => { pick(e.target.files); e.target.value = ""; }}
          />
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-xs">
            <Checkbox checked={replace} onChange={(e) => setReplace(e.target.checked)} />
            {t("kb.replace")}
          </label>
          <Button size="sm" disabled={!files.length || busy} onClick={() => upload.mutate()}>
            {t("kb.importFiles")}
          </Button>
        </div>

        <div className="flex flex-wrap items-end gap-3 border-t border-[var(--color-border)] pt-3">
          <label className="min-w-48 flex-1 space-y-1">
            <span className="text-xs font-medium">{t("kb.prefix")}</span>
            <Input value={pre} onChange={(e) => setPrefix(e.target.value)} placeholder="docs/kb" />
          </label>
          <label className="flex items-center gap-2 pb-2 text-xs">
            <Checkbox checked={follow} onChange={(e) => setFollow(e.target.checked)} />
            {t("kb.follow")}
          </label>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => fromRepo.mutate()}>
            {fromRepo.isPending && <Loader2Icon className="mr-1 h-3.5 w-3.5 animate-spin" />}
            {t("kb.importRepo")}
          </Button>
        </div>

        {binding.imported_at && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" disabled={check.isPending} onClick={() => check.mutate()}>
              <RefreshCwIcon className={cn("mr-1 h-3.5 w-3.5", check.isPending && "animate-spin")} />
              {t("kb.checkAgain")}
            </Button>
            <Button size="sm" variant="outline" disabled={remove.isPending}
                    onClick={async () => {
                      if (await confirm({ title: t("kb.removeConfirm"), danger: true })) remove.mutate();
                    }}>
              <Trash2Icon className="mr-1 h-3.5 w-3.5" /> {t("kb.removeAll")}
            </Button>
          </div>
        )}

        {report && <ReportView report={report} />}
      </CardContent>
    </Card>
  );
}

function ReportView({ report }: { report: KbReport }) {
  const t = useT();
  const rows: [string, string[]][] = [
    [t("kb.reportSkipped"), report.skipped.map((s) => `${s.path}: ${s.reason}`)],
    [t("kb.reportMissing"), report.missing_objects.map((m) => `${m.path}: ${m.objects.join(", ")}`)],
    [t("kb.reportStale"), report.stale],
    [t("kb.reportWarnings"), report.warnings.map((w) => `${w.path}: ${w.message}`)],
  ];
  return (
    <div className="space-y-1 rounded-md bg-[var(--color-muted)]/40 p-3 text-xs" data-testid="kb-report">
      <p>{t("kb.reportSummary", {
        created: report.created.length, updated: report.updated.length,
        unchanged: report.unchanged, deleted: report.deleted.length,
      })}</p>
      {report.embed_failures.length > 0 && <p className="text-[var(--color-warning)]">{t("kb.reportEmbed")}</p>}
      {rows.filter(([, items]) => items.length).map(([label, items]) => (
        <details key={label}>
          <summary className="cursor-pointer">{label} ({items.length})</summary>
          <ul className="ml-4 list-disc font-mono">
            {items.slice(0, 50).map((x) => <li key={x}>{x}</li>)}
          </ul>
        </details>
      ))}
    </div>
  );
}

export { filtersActive };

// ─── filters ─────────────────────────────────────────────────────────────

export function KbFilterBar({ value, onChange, types }: {
  value: KbFilters; onChange: (f: KbFilters) => void; types: string[];
}) {
  const t = useT();
  const set = (k: keyof KbFilters, v: string | boolean) => onChange({ ...value, [k]: v || undefined });
  return (
    <div className="space-y-2" data-testid="kb-filters">
      <div className="grid grid-cols-2 gap-2">
        <Select
          value={value.type ?? ""}
          onChange={(v) => set("type", v)}
          options={[{ value: "", label: t("kb.anyType") }, ...types.map((x) => ({ value: x, label: x }))]}
        />
        <Select
          value={value.status ?? ""}
          onChange={(v) => set("status", v)}
          options={[{ value: "", label: t("kb.anyStatus") },
            ...STATUSES.map((s) => ({ value: s, label: t(`kb.status.${s}`) }))]}
        />
      </div>
      <Input value={value.object ?? ""} placeholder={t("kb.objectPlaceholder")}
             onChange={(e) => set("object", e.target.value)} />
      <Input value={value.where ?? ""} placeholder={t("kb.wherePlaceholder")}
             onChange={(e) => set("where", e.target.value)} />
      <label className="flex items-center gap-2 text-xs">
        <Checkbox checked={!!value.cards_only} onChange={(e) => set("cards_only", e.target.checked)} />
        {t("kb.cardsOnly")}
      </label>
    </div>
  );
}

// ─── one card ────────────────────────────────────────────────────────────

const SHOWN_KEYS = new Set(["type", "id", "title", "status"]);

/** Front matter, 1C objects, staleness and links of the open note. */
export function KbCardPanel({ token, slug, path, onOpen }: {
  token: string | null | undefined; slug: string; path: string; onOpen: (path: string) => void;
}) {
  const t = useT();
  const q = useQuery({
    queryKey: ["kb-note", slug, path],
    queryFn: () => kbApi.note(token!, slug, path),
    enabled: !!token && !!slug && !!path,
  });
  const c: KbCard | undefined = q.data;
  if (!c) return null;
  const st = c.kb.staleness;
  const fm = Object.entries(c.fm ?? {}).filter(([k]) => !SHOWN_KEYS.has(k));
  const links = [...c.links.out.map((l) => ({ ...l, dir: "out" as const })),
    ...c.links.back.map((l) => ({ ...l, dir: "back" as const }))];
  if (!c.card && links.length === 0 && c.links.dangling.length === 0) return null;

  return (
    <div className="mb-4 space-y-3 rounded-md border border-[var(--color-border)] p-3 text-xs print:hidden"
         data-testid="kb-card">
      {c.card && (
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={c.status} />
          {c.origin === "generate" && <Badge variant="info" data-testid="kb-generated-note">{t("kb.generatedNote")}</Badge>}
          {c.kb.status && c.kb.status !== c.status && (
            <span className="text-[var(--color-muted-foreground)]">{t("kb.writtenAs", { status: c.kb.status })}</span>
          )}
          <Badge variant="outline">{c.type}</Badge>
          {c.kb.id && <span className="font-mono">{c.kb.id}</span>}
          {c.kb.source && <span className="font-mono text-[var(--color-muted-foreground)]">{c.kb.source}</span>}
        </div>
      )}
      {st && c.card && (
        <p data-testid="kb-staleness" className={cn(st.state === "stale" && "text-[var(--color-warning)]")}>
          {st.state === "stale" ? t("kb.stale", { objects: (st.changed ?? []).join(", ") })
            : st.state === "fresh" ? t("kb.fresh")
              : t("kb.unknown", { reason: st.reason ?? "" })}
        </p>
      )}
      {c.objects.length > 0 && (
        <div>
          <p className="mb-1 font-medium">{t("kb.objects")}</p>
          <div className="flex flex-wrap gap-1">
            {c.objects.map((o) => (
              <Badge key={o.name} variant={o.found === false ? "destructive" : "outline"}
                     title={o.found === false ? t("kb.notInGraph") : undefined}>
                {o.name}{o.found === false ? " ✕" : ""}
              </Badge>
            ))}
          </div>
        </div>
      )}
      {(c.kb.not_objects?.length ?? 0) > 0 && (
        <p className="text-[var(--color-muted-foreground)]">
          {t("kb.notObjects", { names: (c.kb.not_objects ?? []).join(", ") })}
        </p>
      )}
      {fm.length > 0 && (
        <details>
          <summary className="cursor-pointer font-medium">{t("kb.frontMatter", { count: fm.length })}</summary>
          <dl className="mt-1 grid grid-cols-[minmax(6rem,auto)_1fr] gap-x-3 gap-y-0.5 font-mono">
            {fm.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-[var(--color-muted-foreground)]">{k}</dt>
                <dd className="break-all">{typeof v === "string" ? v : JSON.stringify(v)}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
      {(links.length > 0 || c.links.dangling.length > 0) && (
        <div>
          <p className="mb-1 font-medium">{t("kb.links")}</p>
          <ul className="space-y-0.5">
            {links.map((l) => (
              <li key={`${l.dir}-${l.kind}-${l.repo ?? ""}-${l.path}`}>
                <span className="mr-1 text-[var(--color-muted-foreground)]">
                  {l.dir === "out" ? "→" : "←"} {t(`kb.kind.${l.kind}`)}
                </span>
                {l.repo ? (
                  <span data-testid="kb-foreign-link" title={t("kb.graphExternal")}>
                    <span className="font-mono text-[var(--color-muted-foreground)]">{l.repo}: </span>
                    {l.title || l.path}
                  </span>
                ) : (
                  <button type="button" className="underline-offset-2 hover:underline" onClick={() => onOpen(l.path)}>
                    {l.title || l.path}
                  </button>
                )}
              </li>
            ))}
            {c.links.dangling.map((d) => (
              <li key={`dangling-${d}`} className="text-[var(--color-warning)]">
                {t("kb.dangling", { ref: d })}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ─── links graph ─────────────────────────────────────────────────────────

const NODE_FILL: Record<string, string> = {
  verified: "var(--color-success)", "needs-review": "var(--color-warning)", draft: "var(--color-muted-foreground)",
};

export function KbGraphView({ token, slug, onOpen }: {
  token: string | null | undefined; slug: string; onOpen: (path: string) => void;
}) {
  const t = useT();
  const [cardsOnly, setCardsOnly] = useState(true);
  const q = useQuery({
    queryKey: ["kb-graph", slug],
    queryFn: () => kbApi.graph(token!, slug),
    enabled: !!token && !!slug,
  });
  const graph = useMemo<KbGraph | null>(() => {
    if (!q.data) return null;
    if (!cardsOnly) return q.data;
    const linked = new Set(q.data.edges.flatMap((e) => [e.source, e.target]));
    const nodes = q.data.nodes.filter((n) => n.card || linked.has(n.path));
    const keep = new Set(nodes.map((n) => n.path));
    return { ...q.data, nodes, edges: q.data.edges.filter((e) => keep.has(e.source) && keep.has(e.target)) };
  }, [q.data, cardsOnly]);
  const pos = useMemo(() => (graph ? layout(graph) : new Map()), [graph]);
  const [hover, setHover] = useState<string | null>(null);

  if (!graph) return <p className="text-sm text-[var(--color-muted-foreground)]">{t("kb.loading")}</p>;
  const dangling = Object.entries(graph.dangling);
  return (
    <Card data-testid="kb-graph">
      <CardHeader className="flex flex-row items-center justify-between gap-3 pb-2">
        <CardTitle className="text-sm">
          {t("kb.graphTitle", { nodes: graph.nodes.length, edges: graph.edges.length })}
        </CardTitle>
        <label className="flex items-center gap-2 text-xs">
          <Checkbox checked={cardsOnly} onChange={(e) => setCardsOnly(e.target.checked)} />
          {t("kb.graphCardsOnly")}
        </label>
      </CardHeader>
      <CardContent>
        {graph.nodes.length === 0 ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">{t("kb.graphEmpty")}</p>
        ) : (
          <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full rounded-md bg-[var(--color-muted)]/30"
               role="img" aria-label={t("kb.graphAria")}>
            <defs>
              <marker id="kb-arrow" viewBox="0 0 10 10" refX="16" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                <path d="M0,0 L10,5 L0,10 z" fill="var(--color-border-strong)" />
              </marker>
            </defs>
            {graph.edges.map((e) => {
              const a = pos.get(e.source);
              const b = pos.get(e.target);
              if (!a || !b) return null;
              const lit = hover === e.source || hover === e.target;
              return (
                <line key={`${e.source}>${e.target}>${e.kind}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                      stroke={lit ? "var(--color-primary)" : "var(--color-border-strong)"}
                      strokeWidth={lit ? 1.8 : 1} strokeDasharray={e.kind === "replaces" ? "4 3" : e.kind === "called-by" ? "1 3" : undefined}
                      markerEnd="url(#kb-arrow)" />
              );
            })}
            {graph.nodes.map((n) => {
              const p = pos.get(n.path);
              if (!p) return null;
              return (
                <g key={n.path} transform={`translate(${p.x},${p.y})`} className="cursor-pointer"
                   onMouseEnter={() => setHover(n.path)} onMouseLeave={() => setHover(null)}
                   data-testid={n.external ? "kb-graph-external" : undefined}
                   onClick={() => { if (!n.external) onOpen(n.path); }}>
                  <circle r={n.card ? 7 : 5}
                          fill={n.card ? NODE_FILL[n.status ?? ""] ?? "var(--color-info)" : "var(--color-card)"}
                          stroke={n.external ? "var(--color-primary)" : n.card ? "none" : "var(--color-info)"}
                          strokeWidth={1.5} strokeDasharray={n.external ? "2 2" : undefined} />
                  <text x={10} y={4} fontSize={11} fill="var(--color-foreground)"
                        opacity={hover && hover !== n.path ? 0.45 : 1}>
                    {n.external && n.repo ? `${n.repo}: ` : ""}
                    {n.title.length > 32 ? `${n.title.slice(0, 31)}…` : n.title}
                  </text>
                  <title>{`${n.title}\n${n.path}${n.status ? `\n${n.status}` : ""}${n.external ? `\n${t("kb.graphExternal")}` : ""}`}</title>
                </g>
              );
            })}
          </svg>
        )}
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-[var(--color-muted-foreground)]">
          {STATUSES.map((s) => (
            <span key={s} className="flex items-center gap-1">
              <svg width="10" height="10" aria-hidden><circle cx="5" cy="5" r="5" fill={NODE_FILL[s]} /></svg>
              {t(`kb.status.${s}`)}
            </span>
          ))}
          <span className="flex items-center gap-1">
            <svg width="10" height="10" aria-hidden>
              <circle cx="5" cy="5" r="4" fill="var(--color-card)" stroke="var(--color-info)" strokeWidth="1.5" />
            </svg>
            {t("kb.generated")}
          </span>
          <span>{t("kb.dashedReplaces")}</span>
        </div>
        {graph.omitted > 0 && (
          <p className="mt-1 text-xs text-[var(--color-muted-foreground)]">{t("kb.graphOmitted", { count: graph.omitted })}</p>
        )}
        {dangling.length > 0 && (
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer">{t("kb.danglingTitle", { count: dangling.length })}</summary>
            <ul className="ml-4 list-disc font-mono">
              {dangling.slice(0, 100).map(([p, refs]) => <li key={p}>{p}: {refs.join(", ")}</li>)}
            </ul>
          </details>
        )}
      </CardContent>
    </Card>
  );
}

// ─── changes ─────────────────────────────────────────────────────────────

const SINCE = ["1d", "7d", "30d", "90d"] as const;

export function KbChangesView({ token, slug, onOpen }: {
  token: string | null | undefined; slug: string; onOpen: (path: string) => void;
}) {
  const t = useT();
  const [since, setSince] = useState<string>("7d");
  const q = useQuery({
    queryKey: ["kb-changes", slug, since],
    queryFn: () => kbApi.changes(token!, slug, since),
    enabled: !!token && !!slug,
  });
  const rows = q.data?.changes ?? [];
  return (
    <Card data-testid="kb-changes">
      <CardHeader className="flex flex-row items-center justify-between gap-3 pb-2">
        <CardTitle className="text-sm">{t("kb.changesTitle", { count: rows.length })}</CardTitle>
        <Select value={since} onChange={setSince} className="w-40"
                options={SINCE.map((s) => ({ value: s, label: t(`kb.since.${s}`) }))} />
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-sm text-[var(--color-muted-foreground)]">{t("kb.changesEmpty")}</p>
        ) : (
          <ul className="divide-y divide-[var(--color-border)] text-xs">
            {rows.map((r) => (
              <li key={`${r.path}-${r.at}`} className="flex flex-wrap items-center gap-2 py-1.5">
                <Badge variant={r.change === "stale" ? "warning" : r.change === "deleted" ? "destructive" : "outline"}>
                  {t(`kb.change.${r.change}`)}
                </Badge>
                {r.change === "deleted"
                  ? <span className="font-mono">{r.path}</span>
                  : <button type="button" className="font-mono hover:underline" onClick={() => onOpen(r.path)}>{r.path}</button>}
                <span className="ml-auto text-[var(--color-muted-foreground)]">
                  {new Date(r.at).toLocaleString()}{r.by ? ` · ${r.by}` : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
