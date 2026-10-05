"use client";

/**
 * The agent conversation, as parts: the thread hook and the reply renderers.
 *
 * They lived inside /automation/page.tsx while that page was the only place
 * the agent could be talked to. It is now also a panel that floats over every
 * page (components/agent-widget.tsx), and a second copy of the polling, the
 * session store and the reply cards would be two implementations of one
 * conversation that drift apart the first time either is fixed. So both
 * surfaces import from here, and the rules the page's doc comment states —
 * nothing that costs time runs on the first press, the capabilities message
 * is written down rather than generated, the canned parts of a reply are in
 * the language of the question — are this module's rules.
 *
 * tests/web/test_the_agent_is_a_conversation.py reads this file together with
 * the page, for the same reason.
 */

import Link from "next/link";
import {
  useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";
import {
  AlertTriangleIcon, ArrowRightIcon, CheckCircle2Icon, Loader2Icon,
  MessageSquareIcon, PlayIcon, SquareIcon, XCircleIcon,
} from "lucide-react";

import { api, llmApi } from "@/lib/api";
import { isInAppHref } from "@/lib/in-app-href";
import { AGENT_SESSION_KEY } from "@/lib/agent-session";
import { cn } from "@/lib/utils";
import { useToken } from "@/lib/use-token";
import { useDictFor, useT } from "@/lib/i18n";
import { LocalSetupGuideBody } from "@/components/local-setup-guide";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  VERB_MARKDOWN, VERB_READS, VERB_WRITES, VerbAnswer, VerbOutcome, VerbPreview,
  type VerbPreviewData,
} from "@/components/automation/verb-results";

type Step = {
  action: string | null;
  arguments: Record<string, unknown>;
  note: string;
  resolved_repos: string[];
  blocked: string | null;
  /** For a review-configuration step, the exact change it will make, as the
   *  server computed it from the validators the action runs. Absent on every
   *  other step and on rows written before it existed. */
  preview?: ChangePreviewData;
};

/** One rule as the server will store it. */
type PreviewRule = {
  title: string | null;
  instructions: string;
  path_glob: string | null;
  severity: string | null;
  agents: string[] | null;
};

type ChangePreviewData =
  | { kind: "rules"; repo: string | null; status: "pending" | "active"; rules: PreviewRule[] }
  | { kind: "generate"; repo: string }
  | { kind: "setting"; scope: "workspace" | "repo"; repo: string | null; key: string; value: unknown }
  | VerbPreviewData
  | Record<string, never>;

type RunResult = {
  queued?: { repo: string; job_id: string }[];
  skipped?: { repo: string; reason: string }[];
  changed?: number;
  run_id?: string;
  steps?: { action: string; result: Record<string, unknown> }[];
};

export type Run = {
  id: string;
  session_id: string | null;
  message: string;
  status: "reading" | "planned" | "answered" | "started" | "failed" | "stopped";
  steps: Step[];
  note: string;
  /** ISO 639-1 code of the language the QUESTION was written in, as the
   *  planner read it. Empty when it could not be established — normal, and
   *  the interface language is the fallback. */
  language: string;
  resolved_repos: string[];
  blocked: string | null;
  result: RunResult;
  error: string | null;
  asked_by: string | null;
  created_at: string;
  executed_at: string | null;
  /** The sentence being written, while the row is still "reading". Three
   *  spellings and all of them optional — see `partialOf`. */
  partial_note?: string | null;
  partial_text?: string | null;
  partial?: string | null;
};

export type SessionRow = {
  session_id: string;
  title: string;
  runs: number;
  started_at: string;
  last_at: string;
};

/** Which sitting this browser is in.
 *
 *  localStorage is an external store, so it is read with the hook meant for
 *  one rather than copied into state by an effect: the page is prerendered on
 *  the server, where a lazy initialiser would throw, and setting state from an
 *  effect on mount cascades a second render on every visit.
 *
 *  The snapshot is cached at module level because `getSnapshot` must return
 *  the same value until the store actually changes — generating a fresh uuid
 *  per call would re-render forever.
 */
const SESSION_KEY = AGENT_SESSION_KEY;

let cachedSessionId: string | null = null;
const sessionListeners = new Set<() => void>();

export function newSessionId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  // Insecure origins have no randomUUID. A session id groups rows for reading
  // back; it is never a credential, so this fallback is enough.
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function subscribeSession(onChange: () => void): () => void {
  sessionListeners.add(onChange);
  return () => { sessionListeners.delete(onChange); };
}

function readSessionId(): string {
  if (cachedSessionId) return cachedSessionId;
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(SESSION_KEY);
  } catch {
    /* private mode / storage disabled — a fresh session per load */
  }
  // A reload continues the thread it was in the middle of; only a browser that
  // has never been here starts a new one.
  cachedSessionId = stored || newSessionId();
  return cachedSessionId;
}

/** Nothing on the server, so the first render matches the prerendered HTML and
 *  the queries simply wait one render for the real id. */
function serverSessionId(): string | null {
  return null;
}

function persistSessionId(sid: string): void {
  try {
    localStorage.setItem(SESSION_KEY, sid);
  } catch {
    /* ignore */
  }
}

function switchSession(sid: string): void {
  cachedSessionId = sid;
  persistSessionId(sid);
  sessionListeners.forEach((fn) => fn());
}

/** The catalogue, in the order the canned message reads it out. Kept beside
 *  the page rather than fetched: it exists so that "what can you do" never
 *  costs a model call, and a fetch would put it back on the network. */
const READS = [
  "list_repos", "explain", "help", "audit_status", "list_findings",
  "review_settings", "list_reviews", "get_review_run", "list_issues",
  "ask_code", "search_code",
] as const;
const WRITES = [
  "generate_docs", "start_dep_audit", "set_auto_review",
  "propose_review_rules", "generate_review_rules", "update_review_setting",
  "review_pr", "index_repo", "update_issue",
] as const;

/** The writes that change review configuration rather than queue work over a
 *  set — the server's `CONFIG_VERBS`. Their card shows the change itself
 *  (the rules, the setting and its new value) instead of a repository count,
 *  and their outcome is a saved change with links, not "started on N". */
const CONFIG_VERBS = [
  "propose_review_rules", "generate_review_rules", "update_review_setting",
  "review_pr", "update_issue",
];

function isConfigOnly(steps: { action: string | null }[] | undefined): boolean {
  return !!steps?.length && steps.every((s) => CONFIG_VERBS.includes(s.action ?? ""));
}
const EXAMPLES = [
  "automation.exRead", "automation.ex1", "automation.ex2", "automation.ex3",
] as const;

/** What Celmis is, in the order the paragraphs read. The `explain` verb
 *  answers with a topic and nothing else, precisely so that this text — like
 *  the capabilities message — is a string rather than a model call. */
const PRODUCT = [
  "automation.product.index", "automation.product.answers",
  "automation.product.reviews", "automation.product.audits",
] as const;

/** The model's sentence as far as it has been written.
 *
 *  A reading takes between two and six seconds on the server, and for most of
 *  it the row already holds the sentence while the plan behind it is still
 *  generating. Showing it is the difference between watching a spinner and
 *  reading an answer arrive.
 *
 *  Read defensively, because the field is the server's to name: any of these
 *  absent or empty and this returns "", which is the page exactly as it was
 *  before — a spinner and nothing else. `note` is last and is the important
 *  one: it is where a FINISHED sentence already lives, so a server that
 *  simply writes into it as it goes needs no new field at all. Only ever read
 *  while the row is still "reading"; after that the note is rendered as
 *  itself.
 *
 *  Nothing here goes through a dictionary. It is the model's own prose, so it
 *  is already in the language of the question — the same language the canned
 *  parts of the reply are looked up in.
 */
function partialOf(run: Run): string {
  const text = run.partial_note ?? run.partial_text ?? run.partial ?? run.note;
  return typeof text === "string" ? text.trim() : "";
}

/** How long this browser has been watching the longest-running reading.
 *
 *  Measured here rather than from `created_at`, which is the server's clock:
 *  an offset-less timestamp or an hour of skew reads as a run that started
 *  yesterday, and the poll would back off to its slowest rate on the first
 *  frame of a two-second wait. What the interval wants to know is how long
 *  THIS page has been waiting, which is a question only this page can answer.
 *
 *  Rows that have finished are dropped as they go past, so the map holds at
 *  most the runs currently being read.
 */
function elapsedReading(seen: Map<string, number>, runs: Run[]): number {
  const now = Date.now();
  let longest = 0;
  for (const r of runs) {
    if (r.status !== "reading") {
      seen.delete(r.id);
      continue;
    }
    const since = seen.get(r.id) ?? now;
    seen.set(r.id, since);
    longest = Math.max(longest, now - since);
  }
  return longest;
}

function howMany(result: RunResult | undefined): number {
  return (result?.queued?.length ?? 0) + (result?.changed ?? 0)
    || (result?.run_id ? 1 : 0);
}


/** One conversation with the agent: the thread on the server, the sitting in
 *  this browser, and the three things a person can do to it — ask, stop a
 *  reading, and run a plan.
 *
 *  Shared by the full page and the floating panel, and the sharing is the
 *  point: both read the same session id from the same store, and both write
 *  through the same query keys, so a question asked in the corner of the
 *  repositories page is the newest turn on /automation, and "open full view"
 *  continues the thread instead of starting another one. Two copies of this
 *  would be two answers to "which thread am I in".
 *
 *  Nothing that runs work is called from here except `confirm`, and that
 *  only when the caller presses it — the hook returns the mutation, never
 *  fires it.
 */
export function useAutomationThread() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();

  const sessionId = useSyncExternalStore(
    subscribeSession, readSessionId, serverSessionId,
  );
  const [draft, setDraft] = useState("");
  // Cancelling a plan is a local act: the row stays "planned" on the server so
  // it is still readable as "this was asked and not run".
  const [dismissed, setDismissed] = useState<string[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  // When this browser first saw each run being read, so the poll can back off
  // one that is not going to finish soon. A ref rather than state: it is read
  // by the poll timer and must never cause a render of its own.
  const firstSeen = useRef<Map<string, number>>(new Map());
  // Which thread the transcript has already been dropped to the bottom of.
  const anchored = useRef<string | null>(null);

  // Written back once the id is known — a generated one has to survive the
  // reload, and writing during a snapshot read would be a side effect in
  // render.
  useEffect(() => {
    if (sessionId) persistSessionId(sessionId);
  }, [sessionId]);

  const selectSession = useCallback((sid: string) => {
    switchSession(sid);
    setDraft("");
    setDismissed([]);
  }, []);

  const sessions = useQuery({
    queryKey: ["automation-sessions"],
    queryFn: () => api<SessionRow[]>("/api/automation/sessions?limit=30", { token }),
    enabled: !!token,
  });

  // The whole thread in one request, polled only while something in it is
  // still being read. A page left open on a finished thread asks for nothing.
  //
  // THE RATE IS PART OF THE ANSWER'S LATENCY. Measured on the server, a
  // question reaches a terminal status in 1.8 s to 5.5 s; at 1200 ms the
  // interface added up to another 1.2 s of nothing on top of that — most of
  // the shortest of them. 400 ms is what makes a sentence look like it is
  // arriving rather than appearing.
  //
  // Not at that rate forever, though. A reading still going after half a
  // minute is a long plan or a worker that died holding the row, and neither
  // is worth two and a half requests a second for the rest of the afternoon.
  const history = useQuery({
    queryKey: ["automation-history", sessionId],
    queryFn: () => api<Run[]>(
      `/api/automation/history?limit=50&session_id=${encodeURIComponent(sessionId!)}`,
      { token },
    ),
    enabled: !!token && !!sessionId,
    refetchInterval: (q) => {
      const runs = (q.state.data as Run[] | undefined) ?? [];
      const waited = elapsedReading(firstSeen.current, runs);
      return runs.some((r) => r.status === "reading")
        ? waited < 30_000 ? 400 : waited < 120_000 ? 1500 : 6000
        : false;
    },
  });

  // Newest first on the wire, oldest first on screen: a transcript is read
  // downwards.
  const thread = useMemo(
    () => [...(history.data ?? [])].reverse(),
    [history.data],
  );
  const reading = thread.some((r) => r.status === "reading");
  const lastId = thread.length ? thread[thread.length - 1].id : "";
  const lastStatus = thread.length ? thread[thread.length - 1].status : "";
  // The last turn also grows while a sentence is being written into it.
  // Without this the transcript follows a new turn but not the text in it,
  // and a reply longer than the viewport streams below the fold.
  const lastPartial = thread.length ? partialOf(thread[thread.length - 1]) : "";

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !thread.length) return;
    // A thread OPENS at its newest turn, unconditionally. The tail-following
    // rule below measures a distance that is always large on the first paint
    // of a long transcript, so on its own it left every reopened conversation
    // scrolled to the oldest sentence in it.
    if (anchored.current !== sessionId) {
      anchored.current = sessionId;
      el.scrollTop = el.scrollHeight;
      return;
    }
    // After that, follow the tail only when the reader is already at it —
    // otherwise a poll would yank them off whatever they scrolled back up to
    // read.
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 120) {
      el.scrollTop = el.scrollHeight;
    }
  }, [sessionId, lastId, lastStatus, lastPartial, thread.length]);

  const propose = useMutation({
    mutationFn: (message: string) =>
      api<Run>("/api/automation/plan", {
        method: "POST", token, json: { message, session_id: sessionId },
      }),
    onSuccess: (r) => {
      // Shown before the refetch lands: the round trip is a queue put, and a
      // composer that empties with nothing to show for it reads as a lost
      // message.
      qc.setQueryData<Run[]>(["automation-history", sessionId],
                             (old) => [r, ...(old ?? [])]);
      qc.invalidateQueries({ queryKey: ["automation-history", sessionId] });
      qc.invalidateQueries({ queryKey: ["automation-sessions"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const stop = useMutation({
    mutationFn: (runId: string) =>
      api<Run>(`/api/automation/runs/${runId}/stop`, { method: "POST", token }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["automation-history", sessionId] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const confirm = useMutation({
    mutationFn: (runId: string) =>
      api<RunResult>("/api/automation/execute", {
        method: "POST", token, json: { plan_id: runId },
      }),
    onSuccess: (r) => {
      toast.success(isConfigOnly(r.steps)
        ? t("automation.config.saved")
        : t("automation.started", { count: String(howMany(r)) }));
      qc.invalidateQueries({ queryKey: ["automation-history", sessionId] });
      qc.invalidateQueries({ queryKey: ["automation-sessions"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const send = () => {
    const text = draft.trim();
    if (!text || reading || propose.isPending || !sessionId) return;
    setDraft("");
    propose.mutate(text);
  };

  return {
    token, sessionId, selectSession, sessions, history, thread, reading,
    draft, setDraft, send, dismissed, setDismissed, scrollRef,
    propose, stop, confirm,
  };
}

/** Enter sends; Shift+Enter is a newline. NOT on a touch keyboard, where
 *  Enter is the return key and there is no Shift to hold: a phone has a send
 *  button, and a sentence posted at the first line break buys a model call
 *  and a plan for half a thought. */
export function sendsOnEnter(e: React.KeyboardEvent): boolean {
  const touch = window.matchMedia("(pointer: coarse)").matches;
  return e.key === "Enter" && !e.shiftKey && !touch;
}

/** The model's own prose — a note, or the whole answer to a how-to question —
 *  as markdown, with links into the app as router links.
 *
 *  Only in-app paths become links, and only in the FINISHED note: the server
 *  reduced every link the guide does not name to its words before storing
 *  it. The sentence while it is still being written reaches the screen
 *  before the server has had it to clean, so there a link is its label in
 *  link colour and nothing to press — an invented `/settings/github` must not
 *  be a live button for the seconds until the real note replaces it.
 *  Anything that is not a path on this origin renders as its label only.
 *
 *  Markdown only where the model wrote markdown: a `help` or `review_settings`
 *  answer (the latter is the explanation written from the real settings), or a note
 *  carrying an in-app link (`isMarkdownNote`). Every other note — a plan's
 *  one-liner that echoes `services/*` or `api-*-service` back — is shown
 *  verbatim, as it was before notes could be markdown; parsed, those globs
 *  turn into italics and stored rows would change how they read.
 *
 *  A router link rather than an anchor so pressing one keeps the floating
 *  panel open: it lives in the shell, and a client-side navigation does not
 *  unmount the shell. */
export function isMarkdownNote(text: string, steps?: { action: string | null }[]): boolean {
  return (steps ?? []).some((s) => s.action === "help" || s.action === "review_settings"
    || VERB_MARKDOWN.includes(s.action as (typeof VERB_MARKDOWN)[number]))
    || text.includes("](/");
}

export function NoteText({
  text, className, streaming, markdown,
}: {
  text: string;
  className?: string;
  /** Still being written: a caret follows the last line (globals.css), and
   *  links are not pressable yet. */
  streaming?: boolean;
  /** Parse as markdown (see `isMarkdownNote`); otherwise shown verbatim. */
  markdown: boolean;
}) {
  const box = cn(
    streaming && "celmis-streaming",
    "space-y-1.5 wrap-anywhere text-[13px] leading-relaxed text-[var(--color-foreground)]/90",
    "[&_ol]:list-decimal [&_ol]:space-y-1 [&_ol]:pl-5 [&_ul]:list-disc [&_ul]:space-y-1 [&_ul]:pl-5",
    className,
  );
  if (!markdown) {
    return (
      <div className={box}>
        <p className="whitespace-pre-wrap">{text}</p>
      </div>
    );
  }
  return (
    <div className={box}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) =>
            streaming ? (
              <span className="font-medium text-[var(--color-brand)]">{children}</span>
            ) : isInAppHref(href) ? (
              <Link
                href={href}
                className="font-medium text-[var(--color-brand)] underline decoration-[var(--color-brand)]/40 underline-offset-2 transition-colors hover:decoration-[var(--color-brand)]"
              >
                {children}
              </Link>
            ) : (
              <span>{children}</span>
            ),
          p: ({ children }) => <p className="whitespace-pre-wrap">{children}</p>,
          code: ({ children }) => (
            <code className="rounded bg-[var(--color-muted)] px-1 py-0.5 font-mono text-[0.85em]">
              {children}
            </code>
          ),
          // A heading inside a chat bubble is a shout. The guide is written
          // with them; an answer quoting it should not be.
          h1: ({ children }) => <p className="font-semibold">{children}</p>,
          h2: ({ children }) => <p className="font-semibold">{children}</p>,
          h3: ({ children }) => <p className="font-medium">{children}</p>,
          img: () => null,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

/** The pages a how-to answer pointed at, as buttons under it.
 *
 *  The links in the prose are enough to read; these are for acting. Same
 *  allow-list, computed on the server from the same note. */
function GuideLinks({ links }: { links: { label: string; href: string }[] }) {
  if (links.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {links.map((l) => (
        <Link
          key={l.href}
          href={l.href}
          className="inline-flex items-center gap-1 rounded-full border border-[var(--color-border)] bg-[var(--color-background)] px-2.5 py-1 text-xs font-medium transition-colors hover:border-[var(--color-brand)]/40 hover:bg-[var(--color-accent)] hover:text-[var(--color-brand)]"
        >
          {l.label}
          <ArrowRightIcon className="h-3 w-3" />
        </Link>
      ))}
    </div>
  );
}

/** What came back for one sentence: an answer, a plan awaiting approval, or a
 *  refusal. Rendered from the stored row, so it is the same on a reload. */
export function Reply({
  run, dismissed, stopping, starting, onStop, onConfirm, onCancel, onPick,
}: {
  run: Run;
  dismissed: boolean;
  stopping: boolean;
  starting: boolean;
  onStop: () => void;
  onConfirm: () => void;
  onCancel: () => void;
  onPick: (text: string) => void;
}) {
  const t = useT();
  // Two dictionaries, and the split is the point. `t` is the interface: the
  // buttons this person presses. `said` is the reply: everything written down
  // that is an ANSWER to what they typed, in the language they typed it in.
  const said = useDictFor(run.language);
  const bad = !!(run.blocked || run.error) || run.status === "failed";
  // No steps at all is the model saying it recognised nothing — the one case
  // where the canned list of verbs is the answer.
  const unread = run.status !== "reading" && run.steps.length === 0;
  // What there is of the answer so far. "" on a server that does not send one.
  const partial = partialOf(run);

  /* THE SAME CARD, HALF WRITTEN.
   *
   * Every part of this is in the position the finished reply puts it in: the
   * container, the status line where the title lands, and the sentence in the
   * slot `run.note` renders into, with the same type and the same colour. So
   * when the plan arrives, React reconciles a div onto a div and a <p> onto a
   * <p> — the caret goes, the heading changes, and the sentence is updated in
   * place instead of being torn down and rebuilt somewhere else on screen.
   * Nothing blinks, because nothing is unmounted.
   *
   * That is also why the sentence is BELOW the spinner rather than above it:
   * above, it would have to jump a line down the moment it stopped being
   * partial, which is the flash this arrangement exists to avoid.
   */
  if (run.status === "reading") {
    return (
      <div
        aria-busy
        className="min-w-0 space-y-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-3 py-3"
      >
        <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
          <Loader2Icon className="h-4 w-4 shrink-0 animate-spin text-[var(--color-brand)]" />
          <span className="text-[var(--color-muted-foreground)]">
            {t("automation.reading")}
          </span>
          {/* Stop, not cancel: the reading is a job, and the job stops. It
              stays on the card the whole time the sentence is arriving —
              having something to read is not a reason to lose the way out. */}
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={stopping}
            onClick={onStop}
          >
            <SquareIcon className="mr-1 h-3.5 w-3.5" />
            {stopping ? t("automation.stopping") : t("automation.stop")}
          </Button>
        </div>
        {partial && (
          // `streaming` draws the caret after the last line of text. It says
          // the sentence is not finished, which the spinner says about the
          // reply and not about this line. Decoration only — a screen reader
          // has `aria-busy` for the same fact.
          <NoteText text={partial} streaming markdown={isMarkdownNote(partial, run.steps)} />
        )}
      </div>
    );
  }

  return (
    <div
      className={cn(
        "min-w-0 space-y-3 rounded-lg border px-3 py-3",
        bad
          ? "border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/5"
          : "border-[var(--color-border)] bg-[var(--color-muted)]/30",
      )}
    >
      <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
        {bad
          ? <AlertTriangleIcon className="h-4 w-4 shrink-0 text-[var(--color-destructive)]" />
          : run.status === "answered"
            ? <MessageSquareIcon className="h-4 w-4 shrink-0 text-[var(--color-brand)]" />
            : run.status === "started"
              ? <CheckCircle2Icon className="h-4 w-4 shrink-0 text-[var(--color-brand)]" />
              : run.status === "stopped"
                ? <XCircleIcon className="h-4 w-4 shrink-0 text-[var(--color-muted-foreground)]" />
                : <PlayIcon className="h-4 w-4 shrink-0 text-[var(--color-brand)]" />}
        {run.status === "stopped"
          ? said("automation.status.stopped")
          : run.status === "failed"
            ? said("automation.status.failed")
            : run.status === "answered"
              ? said("automation.answerTitle")
              : unread
                ? said("automation.notUnderstood")
                : run.steps.length === 1
                  ? said(`automation.action.${run.steps[0].action}`)
                  : said("automation.steps", { count: String(run.steps.length) })}
      </div>

      {/* The same paragraph the partial sentence was rendered into, in the
          same slot: this is the element it turns into. */}
      {run.note && <NoteText text={run.note} markdown={isMarkdownNote(run.note, run.steps)} />}

      {(run.blocked || run.error) && (
        <p className="wrap-anywhere text-sm text-[var(--color-destructive)]">
          {run.error || run.blocked}
        </p>
      )}

      {run.status === "answered" && (
        <Answer result={run.result} language={run.language} onPick={onPick} />
      )}

      {/* One block per step. The list, not the count: a misread scope is
          visible here and nowhere else — and with two steps, so is a misread
          pairing of branch to repository. */}
      {run.status === "planned" && run.steps.map((s, i) => (
        <div key={i}
             className="rounded-lg border border-[var(--color-border)] bg-[var(--color-background)]/60 p-3">
          <div className="mb-1 flex flex-wrap items-center gap-2 text-sm font-medium">
            {said(`automation.action.${s.action}`)}
            {!CONFIG_VERBS.includes(s.action ?? "") && (
              <Badge variant="brand" className="text-[10px]">
                {said("automation.repoCount", { count: String(s.resolved_repos.length) })}
              </Badge>
            )}
            {!CONFIG_VERBS.includes(s.action ?? "") && Object.entries(s.arguments)
              .filter(([, v]) => v !== null && v !== undefined && v !== "")
              .filter(([k]) => k !== "repo_slugs" && k !== "owner")
              .map(([k, v]) => (
                <code key={k} className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">
                  {k}: {String(v)}
                </code>
              ))}
          </div>
          {/* The change itself — what Confirm writes — rather than the verb. */}
          {s.preview && "kind" in s.preview && (
            <ChangePreview preview={s.preview} said={said} />
          )}
          {s.note && (
            <p className="mb-1 text-xs text-[var(--color-muted-foreground)]">{s.note}</p>
          )}
          <div className="flex flex-wrap gap-1">
            {s.resolved_repos.map((r) => (
              <code key={r} className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">
                {r}
              </code>
            ))}
          </div>
          {s.blocked && (
            <p className="mt-1 text-xs text-[var(--color-destructive)]">{s.blocked}</p>
          )}
        </div>
      ))}

      {run.status === "planned" && !run.blocked && run.steps.length > 0 && (
        dismissed ? (
          <p className="text-xs text-[var(--color-muted-foreground)]">
            {t("automation.notRun")}
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={starting} onClick={onConfirm}>
              {starting ? t("automation.starting") : t("automation.confirm")}
            </Button>
            <Button size="sm" variant="ghost" onClick={onCancel}>
              {t("automation.cancel")}
            </Button>
          </div>
        )
      )}

      {run.status === "started" && isConfigOnly(run.steps) && (
        <ConfigOutcome result={run.result} said={said} />
      )}

      {run.status === "started" && !isConfigOnly(run.steps) && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
          <span>{said("automation.started", { count: String(howMany(run.result)) })}</span>
          {howMany(run.result) > 0 && (
            <Badge variant="brand" className="text-[10px]">
              {said("automation.queuedCount", { count: String(howMany(run.result)) })}
            </Badge>
          )}
        </div>
      )}

      {/* The review, index and issue writes say more than a count: what was
          skipped and why, and the pages that show the result. */}
      {run.status === "started" && !isConfigOnly(run.steps) && (run.result.steps ?? [])
        .filter((s) => s.action === "index_repo")
        .map((s, i) => (
          <VerbOutcome key={i} action={s.action} result={s.result} t={said} />
        ))}

      {/* Below whatever the model said, never instead of it: its note is the
          only part that is about this particular sentence. In the language of
          that sentence too — this is the reply to a question nobody could
          read, and answering it in a third language helps nobody. */}
      {unread && <Capabilities onPick={onPick} language={run.language} />}
    </div>
  );
}

/** A setting's value as a person reads it: null is "inherit", a list is its
 *  items, anything else as written. */
function settingValue(value: unknown, said: Translate): string {
  if (value === null || value === undefined) return said("automation.config.inherit");
  if (Array.isArray(value)) return value.map(String).join(", ") || "—";
  return String(value);
}

/** What a review-configuration step will write, shown before the press.
 *
 *  The rules exactly as they will be stored — title, severity, the paths they
 *  apply to, the agents they address and the instruction itself — and where
 *  they land: as pending proposals an editor approves, or straight into the
 *  repository's policy. The server decides which from what this build has,
 *  and it is said here so the person knows which one Confirm means. A
 *  setting is its key and its new value. */
function ChangePreview({ preview, said }: { preview: ChangePreviewData; said: Translate }) {
  if (!("kind" in preview)) return null;
  const where = (repo: string | null) => repo ?? said("automation.config.workspace");
  if (preview.kind === "setting") {
    return (
      <div className="mb-1 flex flex-wrap items-center gap-1.5 text-xs">
        <code className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">
          {where(preview.repo)}
        </code>
        <code className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">
          {preview.key}
        </code>
        <ArrowRightIcon className="h-3 w-3 text-[var(--color-muted-foreground)]" />
        <code className="rounded bg-[var(--color-brand)]/10 px-1.5 py-0.5 text-[11px] font-semibold text-[var(--color-brand)]">
          {settingValue(preview.value, said)}
        </code>
      </div>
    );
  }
  if (preview.kind === "review_pr" || preview.kind === "issue") {
    return <VerbPreview preview={preview} t={said} />;
  }
  if (preview.kind === "generate") {
    return (
      <p className="mb-1 text-xs text-[var(--color-muted-foreground)]">
        <code className="mr-1 rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">
          {preview.repo}
        </code>
        {said("automation.config.willGenerate")}
      </p>
    );
  }
  return (
    <div className="mb-1 space-y-2">
      <p className="text-xs text-[var(--color-muted-foreground)]">
        <code className="mr-1 rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">
          {where(preview.repo)}
        </code>
        {preview.status === "pending"
          ? said("automation.config.willPending")
          : said("automation.config.willActive")}
      </p>
      <ol className="list-decimal space-y-1.5 pl-5">
        {preview.rules.map((r, j) => (
          <li key={j} className="text-xs">
            <div className="flex flex-wrap items-center gap-1.5">
              {r.title && <span className="font-medium">{r.title}</span>}
              {r.severity && (
                <Badge variant="outline" className="text-[10px]">{r.severity}</Badge>
              )}
              <code className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">
                {r.path_glob ?? said("automation.config.allFiles")}
              </code>
              {(r.agents ?? []).map((a) => (
                <Badge key={a} variant="outline" className="text-[10px]">{a}</Badge>
              ))}
            </div>
            <p className="mt-0.5 whitespace-pre-wrap wrap-anywhere text-[var(--color-muted-foreground)]">
              {r.instructions}
            </p>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Where a saved review change can be seen. The server names the pages by a
 *  label this table translates; a label it does not know, or an href that is
 *  not a path in this app, is not offered. */
const CONFIG_LINK_LABELS: Record<string, string> = {
  pending: "automation.config.linkPending",
  policy: "automation.config.linkPolicy",
  defaults: "automation.config.linkDefaults",
};

function ConfigOutcome({ result, said }: { result: RunResult; said: Translate }) {
  const steps = result.steps ?? [];
  return (
    <div className="space-y-2">
      {steps.map((s, i) => {
        if ((VERB_WRITES as readonly string[]).includes(s.action)) {
          return <VerbOutcome key={i} action={s.action} result={s.result} t={said} />;
        }
        const r = s.result as {
          status?: string;
          links?: { label: string; href: string }[];
        };
        const links = (Array.isArray(r.links) ? r.links : [])
          .filter((l) => isInAppHref(l.href) && CONFIG_LINK_LABELS[l.label]);
        return (
          <div key={i} className="space-y-1.5">
            <p className="flex items-center gap-1.5 text-xs text-[var(--color-muted-foreground)]">
              <CheckCircle2Icon className="h-3.5 w-3.5 text-[var(--color-brand)]" />
              {r.status === "pending"
                ? said("automation.config.donePending")
                : r.status === "active"
                  ? said("automation.config.doneActive")
                  : said("automation.config.saved")}
            </p>
            {links.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {links.map((l) => (
                  <Link
                    key={l.href}
                    href={l.href}
                    className="inline-flex items-center gap-1 rounded-full border border-[var(--color-border)] bg-[var(--color-background)] px-2.5 py-1 text-xs font-medium transition-colors hover:border-[var(--color-brand)]/40 hover:bg-[var(--color-accent)] hover:text-[var(--color-brand)]"
                  >
                    {said(CONFIG_LINK_LABELS[l.label])}
                    <ArrowRightIcon className="h-3 w-3" />
                  </Link>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** The one message this agent never pays a model to write.
 *
 *  The catalogue, the reads/writes split, and sentences that can be clicked
 *  into the composer — all locale text, so it is free in every language and
 *  identical every time.
 *
 *  `language` is the question's, when there is a question: this is the answer
 *  to "what can you do", and an answer belongs in the language it was asked
 *  in. Absent — the opening turn of an empty thread, which answers nothing —
 *  it is the interface language, which is the only language known yet. */
export function Capabilities({
  onPick, language,
}: {
  onPick: (text: string) => void;
  language?: string;
}) {
  const t = useDictFor(language);

  return (
    <div className="min-w-0 space-y-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-3 py-3 text-sm">
      <p className="font-medium">{t("automation.capabilities.title")}</p>
      <p className="text-[var(--color-muted-foreground)]">
        {t("automation.capabilities.intro")}
      </p>

      <div>
        <p className="text-xs font-medium">{t("automation.capabilities.readsTitle")}</p>
        <ul className="mt-1 space-y-1">
          {READS.map((a) => (
            <li key={a} className="text-xs text-[var(--color-muted-foreground)]">
              <span className="font-medium text-[var(--color-foreground)]">
                {t(`automation.action.${a}`)}
              </span>
              {" — "}
              {t(`automation.capabilities.${a}`)}
            </li>
          ))}
        </ul>
      </div>

      <div>
        <p className="text-xs font-medium">{t("automation.capabilities.writesTitle")}</p>
        <ul className="mt-1 space-y-1">
          {WRITES.map((a) => (
            <li key={a} className="text-xs text-[var(--color-muted-foreground)]">
              <span className="font-medium text-[var(--color-foreground)]">
                {t(`automation.action.${a}`)}
              </span>
              {" — "}
              {t(`automation.capabilities.${a}`)}
            </li>
          ))}
        </ul>
      </div>

      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("automation.readsRunNow")}
      </p>

      <div>
        <p className="text-xs font-medium">{t("automation.capabilities.tryThis")}</p>
        {/* The examples are the documentation: one press puts a working
            sentence in the composer instead of describing one. */}
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {EXAMPLES.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => onPick(t(key))}
              className="rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-2.5 py-1.5 text-left text-xs hover:border-[var(--color-brand)]/40 hover:bg-[var(--color-accent)]"
            >
              {t(key)}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

/** What Celmis is, for somebody who has just been handed it.
 *
 *  The other half of `explain`, and written down for the same reason as the
 *  capabilities message: the answer to "what is this" changes when the
 *  product changes, not when somebody asks, so paying a model to compose it —
 *  in whichever of sixteen languages — buys a paragraph that is already
 *  written. The verb answers with a topic and nothing else; this is the
 *  topic. */
function Product({ language }: { language?: string }) {
  const t = useDictFor(language);

  return (
    <div className="min-w-0 space-y-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-3 py-3 text-sm">
      <p className="font-medium">{t("automation.product.title")}</p>
      {PRODUCT.map((key) => (
        <p key={key} className="text-[var(--color-muted-foreground)]">
          {t(key)}
        </p>
      ))}
      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("automation.product.scope")}
      </p>
    </div>
  );
}

/** A `t()` for the language a reply was written in — see `useDictFor`. The
 *  helpers below take one rather than calling the hook, because they are
 *  reached from a reply and not from the interface. */
type Translate = (key: string, vars?: Record<string, string | number>) => string;

/** A surface, in the words its own card on /settings/llm carries.
 *
 *  The instruction under this is "open the card for the part you want to
 *  move", so the names have to be the ones printed on those cards. Any other
 *  wording sends a reader looking for something that is not on the screen
 *  they were just sent to.
 */
const SURFACE_TITLES: Record<string, string> = {
  chat: "settings.llm.chatTitle",
  review: "settings.llm.reviewTitle",
  agent: "settings.llm.agentTitle",
  embeddings: "settings.llm.embeddingsTitle",
};

/** The surfaces a reply named, spelled for whoever is reading it.
 *
 *  WHICH surfaces those are is the reply's answer and not this file's. On the
 *  server both lists are derived from the rule that refuses a base_url on
 *  everything outside it, so a surface that becomes configurable — or stops
 *  being — moves these sentences without anybody editing sixteen
 *  dictionaries. Said in the paragraphs instead, it would be a second copy:
 *  true today, and wrong the morning that rule changes without them.
 *
 *  Empty for a run answered before the marker existed. Those rows carry
 *  neither field, and no list is rendered rather than a guessed one — the
 *  paragraphs read whole without it, which is why they no longer name it.
 *
 *  A surface this bundle has no card title for is printed as the server
 *  spelled it: that is a newer backend than this frontend, and dropping the
 *  name would be the answer quietly leaving a surface out.
 */
function surfaceNames(raw: unknown, t: Translate): string[] {
  if (!Array.isArray(raw)) return [];
  return (raw as unknown[])
    .filter((s): s is string => typeof s === "string" && s.trim() !== "")
    .map((s) => (SURFACE_TITLES[s] ? t(SURFACE_TITLES[s]) : s));
}

/** One labelled line of surface names, or nothing when there are none. */
function SurfaceList({ label, names }: { label: string; names: string[] }) {
  if (names.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs
                    text-[var(--color-muted-foreground)]">
      {label}
      {names.map((name) => (
        <Badge key={name} variant="outline" className="text-[10px]">{name}</Badge>
      ))}
    </div>
  );
}

/** How to run all of this on your own models — the third `explain` topic.
 *
 *  Prose from here, commands from the server, the split from the reply.
 *
 *  The paragraphs are written down in sixteen languages for the reason
 *  Product and Capabilities are: the answer changes when the product changes,
 *  not when somebody asks, so a model call would be paying to compose a
 *  paragraph that already exists.
 *
 *  The commands are the opposite case. `ollama serve` is `ollama serve` in
 *  every language, a translated flag is a broken one, and the list of servers
 *  worth naming changes on the backend's release schedule rather than the
 *  frontend's. So they arrive from the guide endpoint and are shown verbatim,
 *  in English, inside prose that is not. They are FETCHED, never read out of
 *  the reply: a run's result is written to its row, so a copy carried there
 *  would freeze the commands as they were the day the question was asked.
 */
function SelfHosted({
  result, language,
}: {
  result: Record<string, unknown>;
  language?: string;
}) {
  const t = useDictFor(language);
  const token = useToken();
  const guide = useQuery({
    // The settings page's key, deliberately: the guide is one document, and
    // somebody who opened it there should not wait for it twice.
    queryKey: ["llm-local-setup-guide"],
    queryFn: () => llmApi.localSetupGuide(token!),
    enabled: !!token,
    staleTime: Infinity,
  });
  const uiSurfaces = surfaceNames(result.ui_surfaces, t);
  const envSurfaces = surfaceNames(result.env_surfaces, t);

  return (
    <div className="min-w-0 space-y-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-3 py-3 text-sm">
      <p className="font-medium">{t("automation.selfHosted.title")}</p>
      <p className="text-[var(--color-muted-foreground)]">
        {t("automation.selfHosted.what")}
      </p>
      <p className="text-[var(--color-muted-foreground)]">
        {/* The provider label is quoted from the dropdown's own dictionary
            entry, so the words to look for and the words on the select
            cannot drift into two spellings of one option. */}
        {t("automation.selfHosted.where",
           { option: t("settings.llm.selfHostedOption") })}
      </p>
      <SurfaceList label={t("automation.selfHosted.whereSurfaces")}
                   names={uiSurfaces} />
      <p className="text-[var(--color-muted-foreground)]">
        {t("automation.selfHosted.embeddings")}
      </p>
      <SurfaceList label={t("automation.selfHosted.embeddingsSurfaces")}
                   names={envSurfaces} />
      {guide.isLoading && (
        <p className="text-xs text-[var(--color-muted-foreground)]">
          {t("settings.llm.guideLoading")}
        </p>
      )}
      {guide.error ? (
        // Said out loud, as the settings panel says it. A guide that failed
        // to arrive used to leave this answer with a heading it never
        // reached and no reason why — the reader saw a paragraph, then
        // nothing, and had no way to tell that anything was missing.
        <p className="whitespace-pre-wrap text-xs text-red-700 dark:text-red-400">
          {(guide.error as Error).message}
        </p>
      ) : null}
      {guide.data && (
        <div className="space-y-3">
          <p className="text-xs font-medium">{t("automation.selfHosted.commands")}</p>
          <LocalSetupGuideBody guide={guide.data} t={t} commandsOnly />
        </div>
      )}
      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("automation.selfHosted.reindex")}
      </p>
      <Link href="/settings/llm"
            className="inline-block text-xs underline underline-offset-2">
        {t("automation.selfHosted.settingsLink")}
      </Link>
    </div>
  );
}

/** The result of a read, rendered as the reply it is.
 *
 *  AN EMPTY ANSWER IS NOT AN EMPTY CONVERSATION. Asked "which repositories do
 *  I have" in a workspace with none, this used to reply "Nothing asked yet." —
 *  the sidebar's empty state, borrowed because both places happened to be
 *  empty at once. It answered a question nobody asked, and it said the
 *  question had not been asked, one line under the question. Each read says
 *  what is actually missing: no repositories, no findings, no audit.
 *
 *  All of it is the CONTENT of a reply, so all of it is read out of the
 *  language of the question rather than of the switcher. */
function Answer({
  result, language, onPick,
}: {
  result: RunResult;
  language?: string;
  onPick: (text: string) => void;
}) {
  const t = useDictFor(language);
  const steps = result.steps ?? [];

  return (
    <div className="space-y-3">
      {steps.map((s, i) => {
        const r = s.result as Record<string, any>;

        // The one verb whose entire answer is written down here. The server
        // replies with a topic and nothing else, which is the whole point of
        // it: a question about the product costs no model tokens. An
        // unrecognised topic falls to the product description, which is the
        // answer to the broadest version of the question.
        if (s.action === "explain") {
          if (r.topic === "capabilities") {
            return <Capabilities key={i} onPick={onPick} language={language} />;
          }
          if (r.topic === "self_hosted") {
            return <SelfHosted key={i} result={r} language={language} />;
          }
          return <Product key={i} language={language} />;
        }

        // The answer itself is the note above, written from the product
        // guide; what is left to render is the pages it pointed at.
        if (s.action === "help") {
          return <GuideLinks key={i} links={Array.isArray(r.links) ? r.links : []} />;
        }

        // Same shape: the explanation written from the real settings is the
        // note, and what is left is the page that edits them.
        if (s.action === "review_settings") {
          const links = (Array.isArray(r.links) ? r.links : []).map(
            (l: { href: string }) => ({
              href: l.href, label: t("automation.action.review_settings"),
            }),
          );
          return <GuideLinks key={i} links={links} />;
        }

        if ((VERB_READS as readonly string[]).includes(s.action)) {
          return <VerbAnswer key={i} action={s.action} result={r} t={t} />;
        }

        if (s.action === "list_repos") {
          return (
            <div key={i} className="space-y-1">
              {(r.repos ?? []).map((repo: any) => (
                <div key={repo.repo}
                     className="flex flex-wrap items-center gap-2 rounded-lg border
                                border-[var(--color-border)] px-3 py-2 text-sm">
                  <code className="text-[12px]">{repo.full_name || repo.repo}</code>
                  {repo.branch && (
                    <Badge variant="outline" className="text-[10px]">{repo.branch}</Badge>
                  )}
                  {repo.indexed && (
                    <Badge variant="outline" className="text-[10px]">
                      {t("repositories.indexedBadge")}
                    </Badge>
                  )}
                  {repo.auto_review && (
                    <Badge variant="brand" className="text-[10px]">
                      {t("automation.action.set_auto_review")}
                    </Badge>
                  )}
                </div>
              ))}
              {(r.repos ?? []).length === 0 && (
                <p className="text-xs text-[var(--color-muted-foreground)]">
                  {t("automation.noRepos")}
                </p>
              )}
            </div>
          );
        }

        if (s.action === "list_findings") {
          if ((r.findings ?? []).length === 0) {
            return (
              <p key={i} className="text-xs text-[var(--color-muted-foreground)]">
                {t("automation.noFindings")}
              </p>
            );
          }
          return (
            <ul key={i} className="space-y-1">
              {(r.findings ?? []).slice(0, 25).map((f: any, j: number) => (
                <li key={j} className="flex flex-wrap items-center gap-2 text-xs">
                  <Badge variant="outline" className="text-[10px]">{f.severity}</Badge>
                  <code>{f.package}</code>
                  <span className="text-[var(--color-muted-foreground)]">
                    {f.installed} → {f.latest || "—"}
                  </span>
                  <span className="text-[var(--color-muted-foreground)]">{f.repo}</span>
                </li>
              ))}
            </ul>
          );
        }

        // audit_status and anything added later: the summary as it comes.
        const summary = Object.entries(r.summary ?? r)
          .filter(([, v]) => typeof v !== "object");
        // A run with an empty summary is a workspace that has never audited
        // anything. Named only for the verb it can be true of — a later verb
        // with nothing scalar to show is not "no audits", it is a renderer
        // that has not been written yet.
        if (summary.length === 0 && s.action === "audit_status") {
          return (
            <p key={i} className="text-xs text-[var(--color-muted-foreground)]">
              {t("automation.noAudits")}
            </p>
          );
        }
        return (
          <div key={i} className="flex flex-wrap gap-2 text-xs">
            {summary.map(([k, v]) => (
              <span key={k}
                    className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5">
                {k}: {String(v)}
              </span>
            ))}
          </div>
        );
      })}
    </div>
  );
}
