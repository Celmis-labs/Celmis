"use client";

/**
 * /claude — embedded Claude Code agent.
 *
 * Cursor-style: connect a Claude subscription (setup-token), then run
 * headless agent sessions against a registered repo. Mobile-first — a
 * session started here keeps running server-side after the tab closes.
 */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  PaperclipIcon,
  BotIcon, CheckCircle2Icon, ChevronRightIcon, CircleIcon, Loader2Icon, PlugZapIcon, PlusIcon, XCircleIcon,
} from "lucide-react";

import {
  alertsApi, claudeApi, projectsApi, api, AGENT_MODELS,
  type AgentMode, type AgentSession, type ProjectOut, type RepoOut,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { formatDateTime } from "@/lib/format";
import { useSpotlightOnMount } from "@/lib/tour";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { HelpButton } from "@/components/ui/help-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { QueryState } from "@/components/ui/query-state";
import { Select, type SelectOption } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { WorkspaceBadge } from "@/components/workspace-badge";

export default function ClaudePage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();

  const conn = useQuery({
    queryKey: ["claude-connection"],
    queryFn: () => claudeApi.connection(token!),
    enabled: !!token,
  });
  const sessions = useQuery({
    queryKey: ["agent-sessions"],
    queryFn: () => claudeApi.sessions(token!),
    enabled: !!token,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((s) => s.status === "running" || s.status === "queued")
        ? 5000 : false,
  });
  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });
  // Projects already group the repos that belong together for Q&A. A session
  // asks the same question of the same set, so it offers them as picks rather
  // than making the user re-assemble the group by hand.
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => projectsApi.list(token!),
    enabled: !!token,
  });

  const connected = Boolean(conn.data?.personal || conn.data?.workspace);

  // Onboarding "show me where" lands here for the connection card.
  useSpotlightOnMount();

  // "Fix from here" hand-off: /claude?prompt=…&repo=…&alert=… pre-fills the
  // new-session form with the alert context (client-only, no SSR concerns).
  const [prefill, setPrefill] = useState<{ prompt: string; repo: string; alert: string } | null>(null);
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const prompt = q.get("prompt");
    if (prompt) {
      setPrefill({ prompt, repo: q.get("repo") ?? "", alert: q.get("alert") ?? "" });
      window.history.replaceState(null, "", "/claude");
    }
  }, []);

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<BotIcon className="h-6 w-6" />}
        title={t("claude.title")}
        badge={<WorkspaceBadge />}
        description={t("claude.subtitle")}
        tabs={<SectionTabs set="agent" />}
      />

      {prefill && conn.data && !connected && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-600 dark:text-amber-400">
          <p className="font-medium">{t("claude.prefillNeedsConnection")}</p>
          <p className="mt-1 break-words opacity-80">
            {prefill.prompt.length > 240 ? `${prefill.prompt.slice(0, 240)}…` : prefill.prompt}
          </p>
        </div>
      )}

      {connected && (
        <NewSessionCard
          repos={(repos.data ?? []).map((r) => ({ value: r.slug, label: r.slug }))}
          projects={projects.data ?? []}
          prefill={prefill}
          onCreated={() => qc.invalidateQueries({ queryKey: ["agent-sessions"] })}
        />
      )}

      <ConnectionCard status={conn.data} onChanged={() => qc.invalidateQueries({ queryKey: ["claude-connection"] })} />

      <Card>
        <CardHeader>
          <CardTitle>{t("claude.sessionsTitle")}</CardTitle>
          <CardDescription>{t("claude.sessionsDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <QueryState
            query={sessions}
            empty={{ icon: BotIcon, title: t("claude.noSessions") }}
          >
            {(data) => data.map((s) => (
              <SessionRow key={s.id} session={s} />
            ))}
          </QueryState>
        </CardContent>
      </Card>
    </PageShell>
  );
}

// ─── Connection ──────────────────────────────────────────────────────

function ConnectionCard({
  status, onChanged,
}: {
  status?: { personal: boolean; workspace: boolean; workspace_saved_by?: string | null };
  onChanged: () => void;
}) {
  const t = useT();
  const token = useToken();
  const [open, setOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [scope, setScope] = useState<"personal" | "workspace">("personal");
  const [oat, setOat] = useState("");

  const connect = useMutation({
    mutationFn: () => claudeApi.connect(token!, oat.trim(), scope),
    onSuccess: () => {
      toast.success(t("claude.connected"));
      setOpen(false);
      setOat("");
      onChanged();
    },
    onError: (e) => toast.error((e as Error).message),
  });
  const disconnect = useMutation({
    mutationFn: (s: "personal" | "workspace") => claudeApi.disconnect(token!, s),
    onSuccess: onChanged,
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Card data-tour="claude-connect">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <PlugZapIcon className="h-4 w-4" /> {t("claude.connectionTitle")}
          <HelpButton onClick={() => setHelpOpen(true)} aria-label={t("claude.helpTitle")} />
        </CardTitle>
        <CardDescription>{t("claude.connectionDesc")}</CardDescription>
      </CardHeader>

      <Dialog open={helpOpen} onOpenChange={setHelpOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("claude.helpTitle")}</DialogTitle>
            <DialogDescription>{t("claude.helpIntro")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 text-sm">
            <div>
              <div className="mb-1 font-medium">{t("claude.helpReq")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("claude.helpReqBody")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("claude.helpStep1")}</div>
              <pre className="mb-1 rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/40 px-3 py-2 text-xs">npm install -g @anthropic-ai/claude-code{"\n"}claude setup-token</pre>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("claude.helpStep1Body")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("claude.helpStep2")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("claude.helpStep2Body")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("claude.helpStep3")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("claude.helpStep3Body")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("claude.helpSecurity")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("claude.helpSecurityBody")}</p>
            </div>
            <div>
              <div className="mb-1 font-medium">{t("claude.helpUse")}</div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("claude.helpUseBody")}</p>
            </div>
          </div>
        </DialogContent>
      </Dialog>
      <CardContent className="space-y-3">
        <ConnRow
          label={t("claude.personalToken")}
          connected={Boolean(status?.personal)}
          onDisconnect={() => disconnect.mutate("personal")}
        />
        <ConnRow
          label={t("claude.workspaceToken")}
          hint={status?.workspace_saved_by ? t("claude.savedBy", { email: status.workspace_saved_by }) : undefined}
          connected={Boolean(status?.workspace)}
          onDisconnect={() => disconnect.mutate("workspace")}
        />
        <Button size="sm" onClick={() => setOpen(true)}>
          <PlusIcon className="mr-1 h-3.5 w-3.5" /> {t("claude.connectButton")}
        </Button>
      </CardContent>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("claude.connectTitle")}</DialogTitle>
            <DialogDescription>{t("claude.connectHowTo")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <pre className="rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/40 px-3 py-2 text-xs">
              claude setup-token
            </pre>
            <Select
              value={scope}
              onChange={(v) => setScope(v as "personal" | "workspace")}
              className="w-full"
              options={[
                { value: "personal", label: t("claude.scopePersonal") },
                { value: "workspace", label: t("claude.scopeWorkspace") },
              ]}
            />
            {scope === "workspace" && (
              <p className="text-xs text-amber-500">{t("claude.workspaceWarning")}</p>
            )}
            <Input
              type="password"
              placeholder="sk-ant-oat…"
              value={oat}
              onChange={(e) => setOat(e.target.value)}
            />
            <Button
              className="w-full"
              disabled={!oat.trim() || connect.isPending}
              onClick={() => connect.mutate()}
            >
              {connect.isPending ? t("common.saving") : t("claude.saveToken")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function ConnRow({
  label, hint, connected, onDisconnect,
}: {
  label: string; hint?: string; connected: boolean; onDisconnect: () => void;
}) {
  const t = useT();
  return (
    <div className="flex items-center justify-between gap-3 py-1 text-sm">
      <div className="flex items-center gap-2">
        {connected
          ? <CheckCircle2Icon className="h-4 w-4 text-emerald-500" />
          : <CircleIcon className="h-4 w-4 opacity-40" />}
        <span>{label}</span>
        {hint && <span className="text-xs text-[var(--color-muted-foreground)]">{hint}</span>}
      </div>
      {connected && (
        <Button variant="ghost" size="sm" onClick={onDisconnect}>
          {t("claude.disconnect")}
        </Button>
      )}
    </div>
  );
}

// ─── New session ─────────────────────────────────────────────────────

/** A project is picked through the same select as a repo, so the two are one
 *  namespace: `project:<id>` never collides with a slug, which cannot contain
 *  a colon. */
const PROJECT_PREFIX = "project:";

/** Mirrors MAX_SESSION_REPOS in src/api/routers/claude_code.py. Duplicated on
 *  purpose: the server is the authority and refuses anything over it, this
 *  copy only exists so the user is told before pressing start. */
const MAX_SESSION_REPOS = 5;

function NewSessionCard({
  repos, projects, prefill, onCreated,
}: {
  repos: { value: string; label: string }[];
  projects: ProjectOut[];
  prefill: { prompt: string; repo: string; alert: string } | null;
  onCreated: () => void;
}) {
  const t = useT();
  const token = useToken();
  // Either a slug or `project:<id>` — one select, because "what should the
  // agent see" is one question and asking it twice invites contradictory
  // answers.
  const [repo, setRepo] = useState("");
  // Repos cloned ALONGSIDE the primary one. Kept separate from `repo` so the
  // common single-repo flow is unchanged: pick one, press start.
  const [extraRepos, setExtraRepos] = useState<string[]>([]);
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState<AgentMode>("standard");
  const [model, setModel] = useState("");
  // `field-sizing: content` only landed in Safari 26 — on the iOS versions in
  // the wild today the CSS is inert, so the box has to be grown by hand.
  const promptRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = promptRef.current;
    if (!el || CSS.supports?.("field-sizing", "content")) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, window.innerHeight * 0.4)}px`;
  }, [prompt]);

  useEffect(() => {
    if (!prefill) return;
    setPrompt(prefill.prompt);
    // Match the alert's repo hint against registered slugs (exact or substring).
    if (prefill.repo) {
      const hit = repos.find(
        (r) => r.value === prefill.repo || r.value.includes(prefill.repo.replace("/", "-")),
      );
      if (hit) setRepo(hit.value);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill, repos.length]);

  const pickedProject = repo.startsWith(PROJECT_PREFIX)
    ? projects.find((p) => p.id === repo.slice(PROJECT_PREFIX.length)) ?? null
    : null;
  // What the pick itself brings in: every repo of a project, in the order it
  // was added, or the single repo. Members the workspace no longer registers
  // are NOT filtered out — the server refuses them by name, which the user can
  // act on, whereas quietly auditing a subset of the project it named is the
  // kind of silence nobody notices.
  // A project that vanished between render and submit (deleted elsewhere,
  // refetched away) leaves the sentinel selected with nothing behind it. The
  // sentinel must never resolve to itself — it would travel to the server as a
  // repository named "project:<uuid>".
  const projectPicked = repo.startsWith(PROJECT_PREFIX);
  const projectGone = projectPicked && !pickedProject;
  const projectEmpty = Boolean(pickedProject) && pickedProject!.repos.length === 0;
  const baseSlugs = projectPicked
    ? pickedProject?.repos.map((r) => r.repo_slug) ?? []
    : repo
      ? [repo]
      : [];
  const extras = extraRepos.filter((s) => !baseSlugs.includes(s));
  // With nothing selected the add-ons are not a session of their own: a stale
  // chip list would otherwise start a run against a repo the user only ever
  // marked as secondary, while the picker showed empty.
  const allSlugs = baseSlugs.length ? [...baseSlugs, ...extras] : [];
  const overLimit = allSlugs.length > MAX_SESSION_REPOS;
  const chipCandidates = repos.filter((r) => !baseSlugs.includes(r.value));

  // Changing the pick invalidates the add-ons: they were chosen against the
  // previous selection, and silently carrying them over is how a session ends
  // up cloning a repo nobody asked for.
  const pick = (v: string) => {
    setRepo(v);
    setExtraRepos([]);
  };

  const options: SelectOption[] = [
    ...projects.map((p) => ({
      value: `${PROJECT_PREFIX}${p.id}`,
      label: p.name,
      group: t("nav.projects"),
      hint: t("claude.projectRepoCount", { count: p.repos.length }),
    })),
    ...repos.map((r) => ({ ...r, group: t("nav.repositories") })),
  ];

  // Files attached BEFORE the session exists. They are uploaded straight away
  // and held under a server-minted staging id, which the create call carries;
  // the runner then moves them into the clone and names them in the first
  // prompt. Holding them in memory until submit instead would lose them to a
  // refresh and put a multi-megabyte screenshot in React state.
  const [stagingId, setStagingId] = useState<string | null>(null);
  const [staged, setStaged] = useState<{ name: string; bytes: number }[]>([]);
  const [staging, setStaging] = useState(false);

  const create = useMutation({
    // A project pick sends the project and the ADD-ONS, never the expansion:
    // this list was read when the page rendered, and the server re-reads
    // membership at submit time precisely so a repo dropped from the project
    // meanwhile is not cloned. Sending both would union the two and resurrect
    // it.
    mutationFn: () =>
      claudeApi.createSession(token!, pickedProject ? "" : allSlugs[0], prompt.trim(), {
        mode, model,
        repo_slugs: pickedProject ? extras : allSlugs,
        project_id: pickedProject?.id ?? null,
        staging_id: stagingId,
      }),
    onSuccess: (s) => {
      toast.success(t("claude.sessionStarted"));
      setPrompt("");
      setStaged([]);
      setStagingId(null);
      if (prefill?.alert && token) {
        void alertsApi.patch(token, prefill.alert, { status: "acked", session_id: s.id })
          .catch(() => undefined);
      }
      onCreated();
      window.location.href = `/claude/${s.id}`;
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("claude.newSessionTitle")}</CardTitle>
        <CardDescription className="hidden sm:block">{t("claude.newSessionDesc")}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Select
          value={repo}
          onChange={pick}
          options={options}
          className="w-full"
          placeholder={t(projects.length ? "claude.pickRepoOrProject" : "claude.pickRepo")}
        />
        <Textarea
          ref={promptRef}
          placeholder={t("claude.promptPlaceholder")}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          className="field-sizing-content max-h-[40vh] min-h-24 overflow-y-auto"
        />
        {baseSlugs.length > 0 && chipCandidates.length > 0 && (
          <div className="space-y-1">
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("claude.alsoClone")}
            </p>
            <div className="flex flex-wrap gap-2">
              {chipCandidates
                .map((r) => {
                  const on = extraRepos.includes(r.value);
                  return (
                    <Button
                      key={r.value}
                      type="button"
                      variant={on ? "default" : "outline"}
                      size="sm"
                      className="max-w-full"
                      onClick={() =>
                        setExtraRepos((cur) =>
                          on ? cur.filter((v) => v !== r.value) : [...cur, r.value],
                        )
                      }
                    >
                      <span className="truncate">{r.label}</span>
                    </Button>
                  );
                })}
            </div>
          </div>
        )}

        {/* Outside the chips block on purpose. These explain why Start is
            disabled, and the chips block is gated on there being another repo
            to offer — a condition that has nothing to do with either. */}
        {allSlugs.length > 1 && !overLimit && (
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("claude.alsoCloneHint", { count: allSlugs.length })}
          </p>
        )}
        {overLimit && (
          <p className="text-xs text-[var(--color-destructive)]">
            {t("claude.tooManyRepos", { count: allSlugs.length, max: MAX_SESSION_REPOS })}
          </p>
        )}
        {(projectEmpty || projectGone) && (
          <p className="text-xs text-[var(--color-destructive)]">
            {t(projectEmpty ? "claude.projectEmpty" : "claude.projectGone")}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <Select
              value={mode}
              onChange={(v) => setMode(v as AgentMode)}
              options={[
                { value: "standard", label: t("claude.modeStandard") },
                { value: "workflow", label: t("claude.modeWorkflow") },
              ]}
              className="w-full"
            />
            <p className="hidden text-xs text-[var(--color-muted-foreground)] sm:block">
              {t(mode === "workflow" ? "claude.modeWorkflowHint" : "claude.modeStandardHint")}
            </p>
          </div>
          <div className="space-y-1">
            <Select
              value={model}
              onChange={setModel}
              options={AGENT_MODELS.map((m) => ({
                value: m,
                label: m === "" ? t("claude.modelDefault") : t(`claude.model.${m}`),
              }))}
              className="w-full"
            />
            <p className="hidden text-xs text-[var(--color-muted-foreground)] sm:block">
              {t("claude.modelHint")}
            </p>
          </div>
        </div>
        {/* Attach BEFORE starting. The paperclip used to exist only inside a
            running session, which is the wrong moment: the thing people want
            to attach is the production log or the screenshot that made them
            open the session at all. */}
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex h-9 cursor-pointer items-center gap-1.5 rounded-md border border-[var(--color-border)] px-3 text-sm text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]">
            {staging
              ? <Loader2Icon className="h-4 w-4 animate-spin" />
              : <PaperclipIcon className="h-4 w-4" />}
            {t("claude.attachBefore")}
            <input
              type="file"
              multiple
              className="hidden"
              accept=".txt,.log,.md,.csv,.tsv,.json,.yaml,.yml,.diff,.patch,.sql,.xml,.ini,.toml,.conf,.png,.jpg,.jpeg,.gif,.webp"
              onChange={async (e) => {
                const files = Array.from(e.target.files || []);
                e.target.value = "";
                if (!files.length || !token) return;
                setStaging(true);
                let id = stagingId;
                try {
                  for (const f of files) {
                    const r = await claudeApi.stageAttachment(token, f, id);
                    id = r.staging_id;
                    // Recorded per file, not once after the loop. It used to
                    // sit after it, inside the try — so one rejected file
                    // (wrong type, over the size cap) threw past the
                    // assignment and discarded the id the SUCCESSFUL uploads
                    // had been minted under. Their badges stayed on screen,
                    // createSession sent staging_id: null, and the server
                    // never claimed the directory: files shown as attached
                    // that the agent never saw.
                    setStagingId(id);
                    setStaged((cur) => [...cur, { name: r.name, bytes: r.bytes }]);
                  }
                } catch (err) {
                  toast.error((err as Error).message);
                } finally {
                  setStaging(false);
                }
              }}
            />
          </label>
          {staged.map((f) => (
            <Badge key={f.name} variant="outline" className="text-[10px]">
              {f.name} · {Math.max(1, Math.round(f.bytes / 1024))} KB
            </Badge>
          ))}
        </div>
        <Button
          className="w-full sm:w-auto"
          disabled={
            allSlugs.length === 0 || overLimit
            || prompt.trim().length < 3 || create.isPending || staging
          }
          onClick={() => create.mutate()}
        >
          {create.isPending
            ? <Loader2Icon className="mr-1 h-3.5 w-3.5 animate-spin" />
            : <BotIcon className="mr-1 h-3.5 w-3.5" />}
          {t("claude.startSession")}
        </Button>
      </CardContent>
    </Card>
  );
}

// ─── Session row ─────────────────────────────────────────────────────

function SessionRow({ session }: { session: AgentSession }) {
  const badge =
    session.status === "done" ? (
      <Badge variant="success">done</Badge>
    ) : session.status === "error" ? (
      <Badge variant="destructive">error</Badge>
    ) : session.status === "cancelled" ? (
      <Badge>cancelled</Badge>
    ) : (
      <Badge variant="brand">
        <Loader2Icon className="mr-1 h-3 w-3 animate-spin" /> {session.status}
      </Badge>
    );

  return (
    <Link
      href={`/claude/${session.id}`}
      className="flex items-center justify-between gap-3 rounded-md border border-[var(--color-border)] p-3 transition-colors hover:bg-[var(--color-accent)]"
    >
      <div className="min-w-0">
        <div className="truncate text-sm font-medium">{session.title || session.prompt}</div>
        <div className="text-xs text-[var(--color-muted-foreground)]">
          {session.repo_slug} · {formatDateTime(session.created_at)}
          {session.created_by ? ` · ${session.created_by}` : ""}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {session.status === "error" && <XCircleIcon className="h-4 w-4 text-red-500" />}
        {badge}
        <ChevronRightIcon className="h-4 w-4 opacity-50" />
      </div>
    </Link>
  );
}
