"use client";

/**
 * The team's own list of business processes for a repository (`/api/kb/{slug}/processes`).
 *
 * Each row names a process, says what it is and lists its starting objects (1C
 * objects in any spelling, or code symbols / paths for other repositories).
 * "Generate" writes ONE note per process into the repository's documentation;
 * such a note is a draft until someone checks it, and a verified card in a
 * knowledge-base repository can replace it.
 */

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FileUpIcon, Loader2Icon, PlusIcon, SparklesIcon, Trash2Icon } from "lucide-react";

import { ApiError, llmApi, processesApi, type ProcessList, type ProcessRow, type ProcessSpec } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { StatusBadge } from "@/components/docs/knowledge-base";

const ACCEPT = ".json,.yaml,.yml,.csv";

/** Same languages the repositories page offers for the vault. */
const LANGS = [
  ["uk", "Українська"], ["en", "English"], ["pl", "Polski"], ["de", "Deutsch"], ["fr", "Français"],
  ["es", "Español"], ["pt", "Português"], ["it", "Italiano"], ["nl", "Nederlands"], ["cs", "Čeština"],
  ["sk", "Slovenčina"], ["ro", "Română"], ["hu", "Magyar"], ["bg", "Български"], ["el", "Ελληνικά"],
  ["tr", "Türkçe"], ["sv", "Svenska"], ["no", "Norsk"], ["da", "Dansk"], ["fi", "Suomi"],
  ["ja", "日本語"], ["ko", "한국어"], ["zh-CN", "简体中文"], ["zh-TW", "繁體中文"],
].map(([value, label]) => ({ value, label }));

/** A row being edited: the objects are one text box, one object per line. */
type Draft = { key: string; id: string; name: string; description: string; objects: string };

const toDraft = (p: ProcessRow | ProcessSpec): Draft => ({
  key: p.id, id: p.id, name: p.name, description: p.description ?? "", objects: (p.objects ?? []).join("\n"),
});
const toSpec = (d: Draft): ProcessSpec => ({
  id: d.id, name: d.name.trim(), description: d.description.trim(),
  objects: d.objects.split("\n").map((o) => o.trim()).filter(Boolean),
});

/** Why a refused list was refused: the server names the row. */
export function problemsOf(e: unknown): string {
  if (e instanceof ApiError) {
    const detail = (e.body as { detail?: { problems?: string[] } | string } | null)?.detail;
    if (detail && typeof detail === "object" && Array.isArray(detail.problems)) return detail.problems.join("\n");
  }
  return (e as Error).message;
}

export function ProcessesPanel({ token, slug, onOpen }: {
  token: string | null | undefined; slug: string; onOpen: (path: string) => void;
}) {
  const t = useT();
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [rows, setRows] = useState<Draft[] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [replace, setReplace] = useState(false);
  const [engine, setEngine] = useState<string | null>(null);
  const [lang, setLang] = useState<string | null>(null);
  const [problems, setProblems] = useState("");
  const [waitingFor, setWaitingFor] = useState<string | null>(null);

  const cfg = useQuery({ queryKey: ["llm-config"], queryFn: () => llmApi.getConfig(token!), enabled: !!token });
  const list = useQuery({
    queryKey: ["processes", slug],
    queryFn: async () => {
      const r = await processesApi.list(token!, slug);
      // notes appear (and their status changes) while the run goes on
      if (waitingFor) {
        void qc.invalidateQueries({ queryKey: ["docs", slug] });
        void qc.invalidateQueries({ queryKey: ["kb", slug] });
      }
      return r;
    },
    enabled: !!token && !!slug,
    // While a generation runs, look again until the run that we started is recorded.
    refetchInterval: (q) => (waitingFor && q.state.data?.last_run?.job_id !== waitingFor ? 4000 : false),
  });
  const data: ProcessList | undefined = list.data;
  const stored = data?.processes ?? [];
  const shown: Draft[] = rows ?? stored.map(toDraft);
  const dirty = rows !== null;
  const limits = data?.limits;
  const run = data?.last_run;
  const running = !!waitingFor && run?.job_id !== waitingFor;
  const apply = (r: ProcessList) => {
    qc.setQueryData(["processes", slug], r);
    setRows(null);
    setProblems("");
  };
  const refused = (e: unknown) => { setProblems(problemsOf(e)); toast.error(t("proc.refused")); };

  const save = useMutation({
    mutationFn: () => processesApi.save(token!, slug, shown.map(toSpec)),
    onSuccess: (r) => { apply(r); toast.success(t("proc.saved", { count: r.processes.length })); },
    onError: refused,
  });
  const upload = useMutation({
    mutationFn: (file: File) => processesApi.upload(token!, slug, file, replace),
    onSuccess: (r) => { apply(r); toast.success(t("proc.imported", { count: r.processes.length })); },
    onError: refused,
  });
  const remove = useMutation({
    mutationFn: (id: string) => processesApi.remove(token!, slug, id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["processes", slug] });
      void qc.invalidateQueries({ queryKey: ["docs", slug] });
    },
    onError: (e) => toast.error((e as Error).message),
  });
  const generate = useMutation({
    mutationFn: (ids: string[]) => processesApi.generate(token!, slug, {
      ids: ids.length ? ids : undefined, engine: engine ?? cfg.data?.docs_engine ?? "api",
      language: lang ?? cfg.data?.docs_language ?? "uk",
    }),
    onSuccess: (r) => {
      toast.success(r.queued ? t("proc.queued", { count: r.count ?? 0 }) : r.detail);
      setWaitingFor(r.queued ? r.job_id : null);
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const edit = (key: string, patch: Partial<Draft>) =>
    setRows(shown.map((d) => (d.key === key ? { ...d, ...patch } : d)));
  const add = () => setRows([...shown, { key: `new-${Date.now()}`, id: "", name: "", description: "", objects: "" }]);
  const drop = (d: Draft) => {
    if (stored.some((s) => s.id === d.id && d.key === d.id)) remove.mutate(d.id);
    setRows(shown.filter((x) => x.key !== d.key));
    setPicked((p) => { const n = new Set(p); n.delete(d.id); return n; });
  };
  const byId = new Map(stored.map((s) => [s.id, s]));
  const full = !!limits && shown.length >= limits.processes;
  const ids = [...picked];

  return (
    <Card className="print:hidden" data-testid="proc-panel">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">{t("proc.title")}</CardTitle>
        <CardDescription>{t("proc.help")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {shown.length === 0 && <p className="text-xs text-[var(--color-muted-foreground)]">{t("proc.empty")}</p>}
        <div className="space-y-3">
          {shown.map((d) => {
            const s: ProcessRow | undefined = byId.get(d.id);
            const note = s?.note;
            return (
              <div key={d.key} className="space-y-2 rounded-md border border-[var(--color-border)] p-3"
                   data-testid="proc-row">
                <div className="flex flex-wrap items-center gap-2">
                  {s && (
                    <Checkbox checked={picked.has(d.id)} aria-label={t("proc.select")}
                              onChange={(e) => setPicked((p) => {
                                const n = new Set(p);
                                if (e.target.checked) n.add(d.id); else n.delete(d.id);
                                return n;
                              })} />
                  )}
                  <Input value={d.name} onChange={(e) => edit(d.key, { name: e.target.value })}
                         placeholder={t("proc.name")} maxLength={limits?.name} className="min-w-48 flex-1"
                         aria-label={t("proc.name")} />
                  {note && (
                    <>
                      <StatusBadge status={note.effective_status} />
                      <Button size="sm" variant="ghost" onClick={() => onOpen(note.path)} data-testid="proc-open">
                        {t("proc.openNote")}
                      </Button>
                    </>
                  )}
                  {s?.result?.status === "failed" && (
                    <Badge variant="destructive" title={s.result.error}>{t("proc.failed")}</Badge>
                  )}
                  <Button size="sm" variant="ghost" onClick={() => drop(d)} aria-label={t("proc.remove")}
                          data-testid="proc-remove">
                    <Trash2Icon className="h-3.5 w-3.5" />
                  </Button>
                </div>
                <Textarea value={d.description} rows={2} placeholder={t("proc.description")}
                          maxLength={limits?.description} aria-label={t("proc.description")}
                          onChange={(e) => edit(d.key, { description: e.target.value })} />
                <Textarea value={d.objects} rows={3} placeholder={t("proc.objectsPlaceholder")}
                          aria-label={t("proc.objects")} className="font-mono text-xs"
                          onChange={(e) => edit(d.key, { objects: e.target.value })} />
                {note && note.objects_missing.length > 0 && (
                  <p className="text-xs text-[var(--color-warning)]">
                    {t("proc.missing", { objects: note.objects_missing.join(", ") })}
                  </p>
                )}
              </div>
            );
          })}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={add} disabled={full} data-testid="proc-add">
            <PlusIcon className="mr-1 h-3.5 w-3.5" /> {t("proc.add")}
          </Button>
          <Button size="sm" onClick={() => save.mutate()} disabled={!dirty || save.isPending} data-testid="proc-save">
            {save.isPending && <Loader2Icon className="mr-1 h-3.5 w-3.5 animate-spin" />} {t("proc.save")}
          </Button>
          {limits && (
            <span className="text-xs text-[var(--color-muted-foreground)]">
              {t("proc.limits", { max: limits.processes, objects: limits.objects })}
            </span>
          )}
        </div>
        {problems && (
          <pre className="whitespace-pre-wrap rounded-md bg-[var(--color-destructive-soft)] p-2 text-xs text-[var(--color-destructive)]"
               role="alert" data-testid="proc-problems">{problems}</pre>
        )}

        <div className="flex flex-wrap items-center gap-3 border-t border-[var(--color-border)] pt-3">
          <Button size="sm" variant="outline" onClick={() => input.current?.click()} disabled={upload.isPending}
                  data-testid="proc-import">
            {upload.isPending ? <Loader2Icon className="mr-1 h-3.5 w-3.5 animate-spin" />
              : <FileUpIcon className="mr-1 h-3.5 w-3.5" />}
            {t("proc.import")}
          </Button>
          <input ref={input} type="file" accept={ACCEPT} className="hidden" data-testid="proc-input"
                 onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = ""; }} />
          <label className="flex items-center gap-2 text-xs">
            <Checkbox checked={replace} onChange={(e) => setReplace(e.target.checked)} />
            {t("proc.replace")}
          </label>
          <span className="text-xs text-[var(--color-muted-foreground)]">{t("proc.importHelp")}</span>
        </div>

        <div className="flex flex-wrap items-end gap-3 border-t border-[var(--color-border)] pt-3">
          <label className="space-y-1">
            <span className="text-xs font-medium">{t("proc.engine")}</span>
            <Select className="w-44" value={engine ?? cfg.data?.docs_engine ?? "api"} onChange={setEngine}
                    options={[{ value: "api", label: t("repositories.vaultEngineApi") },
                      { value: "claude_code", label: t("repositories.vaultEngineAgent") }]} />
          </label>
          <label className="space-y-1">
            <span className="text-xs font-medium">{t("proc.language")}</span>
            <Select className="w-44" value={lang ?? cfg.data?.docs_language ?? "uk"} onChange={setLang}
                    options={LANGS} />
          </label>
          <Button size="sm" disabled={!stored.length || dirty || generate.isPending || running}
                  onClick={() => generate.mutate([])} data-testid="proc-generate-all">
            <SparklesIcon className="mr-1 h-3.5 w-3.5" /> {t("proc.generateAll")}
          </Button>
          <Button size="sm" variant="outline" disabled={!ids.length || dirty || generate.isPending || running}
                  onClick={() => generate.mutate(ids)} data-testid="proc-generate-selected">
            {t("proc.generateSelected", { count: ids.length })}
          </Button>
        </div>
        {dirty && <p className="text-xs text-[var(--color-muted-foreground)]">{t("proc.saveFirst")}</p>}

        {running && (
          <p className="flex items-center gap-2 text-xs" data-testid="proc-running">
            <Loader2Icon className="h-3.5 w-3.5 animate-spin" /> {t("proc.running")}
          </p>
        )}
        {run?.finished_at && !running && (
          <p className="text-xs text-[var(--color-muted-foreground)]" data-testid="proc-last-run">
            {t("proc.lastRun", {
              when: new Date(run.finished_at).toLocaleString(),
              ok: stored.filter((s) => s.result?.status === "ok").length,
              failed: stored.filter((s) => s.result?.status === "failed").length,
            })}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
