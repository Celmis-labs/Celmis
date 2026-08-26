"use client";

/**
 * A conversation, not a form.
 *
 * Deliberately narrow on the writing side. Single actions belong on buttons: a
 * form with three fields beats guessing whether a model read "in German"
 * correctly, and generating documentation for one repository is a
 * once-per-repository event.
 *
 * What a form answers badly is a set defined by a CONDITION. "Every service
 * that has no documentation" is one line here and forty checkboxes there.
 *
 * NOTHING THAT COSTS TIME RUNS ON THE FIRST PRESS. Those verbs cost money and
 * hours, and interpretation is a guess. The plan is where a misreading becomes
 * visible: "all of them" meaning forty repositories instead of four is obvious
 * in a list and invisible in a sentence. Questions are the exception — they
 * cost nothing and touch nothing, so they answer immediately.
 *
 * Why a transcript. The page used to show one question, one card, and a list of
 * everything ever asked underneath, which is a form with a receipt printer. A
 * plan is a reply to a sentence, and a reply belongs under the sentence it
 * answers — read back in order, a thread also shows what was already tried
 * before somebody asks for it again.
 *
 * Nothing here lives in this component. The reading runs on the queue and the
 * row is on the server: close the page mid-thought and the answer is waiting
 * when you come back, and the Stop button works because a job is the one thing
 * in this system that can be asked to stop. The session id is the exception —
 * it is the browser's idea of where one sitting ends, so it is generated here
 * and kept in localStorage; without that, a reload would orphan the thread it
 * was halfway through.
 *
 * THE CAPABILITIES MESSAGE IS WRITTEN DOWN, NOT GENERATED. "What can you do"
 * has one answer, it changes only when the catalogue does, and paying a model
 * call to recite six verbs — in whichever of sixteen languages was asked — is
 * money spent on a string that could be a string. So it is locale text,
 * rendered client-side: as the opening turn of an empty thread, and again
 * under any reply that recognised no action at all.
 *
 * AND IT IS RENDERED IN THE LANGUAGE OF THE QUESTION, not of the interface.
 * The model's own note comes back in the language it was asked in, because it
 * is generated; the written-down parts used to come back in whatever the
 * person had picked in the switcher, so a Ukrainian question was answered by a
 * Ukrainian sentence with an English panel bolted underneath it. Those strings
 * exist in sixteen languages so that saying them is free — saying them in the
 * wrong one spends the translation and delivers nothing. Every canned part of
 * a reply is looked up with `useDictFor(run.language)`; the furniture around
 * the conversation — the composer, the buttons, the session list — stays in
 * the interface language, which is what the person actually chose.
 */

import Link from "next/link";
import {
  useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangleIcon, CheckCircle2Icon, ClockIcon, Loader2Icon,
  MessageSquareIcon, PlayIcon, PlusIcon, SendIcon, SquareIcon, Trash2Icon,
  WandIcon, XCircleIcon, XIcon,
} from "lucide-react";

import { api, llmApi } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useToken } from "@/lib/use-token";
import { useDictFor, useT } from "@/lib/i18n";
import { LocalSetupGuideBody } from "@/components/local-setup-guide";
import { PageHeader, PageShell } from "@/components/page-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";

type Step = {
  action: string | null;
  arguments: Record<string, unknown>;
  note: string;
  resolved_repos: string[];
  blocked: string | null;
};

type RunResult = {
  queued?: { repo: string; job_id: string }[];
  skipped?: { repo: string; reason: string }[];
  changed?: number;
  run_id?: string;
  steps?: { action: string; result: Record<string, unknown> }[];
};

type Run = {
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

type SessionRow = {
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
const SESSION_KEY = "automation_session_id";

let cachedSessionId: string | null = null;
const sessionListeners = new Set<() => void>();

function newSessionId(): string {
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
const READS = ["list_repos", "explain", "audit_status", "list_findings"] as const;
const WRITES = ["generate_docs", "start_dep_audit", "set_auto_review"] as const;
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

export default function AutomationPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  // Aliased on purpose: `confirm` on this page is already the mutation that
  // RUNS a plan. A `confirm` that asks and a `confirm` that starts an hour of
  // model time, declared four lines apart, is the one name collision in this
  // file worth spending a rename on.
  const { confirm: askFirst, dialog: confirmDialog } = useConfirm();

  const sessionId = useSyncExternalStore(
    subscribeSession, readSessionId, serverSessionId,
  );
  const [draft, setDraft] = useState("");
  const [sessionsOpen, setSessionsOpen] = useState(false);
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
    setSessionsOpen(false);
    setDraft("");
    setDismissed([]);
  }, []);

  const startNewChat = () => {
    const sid = newSessionId();
    selectSession(sid);
  };

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

  // What the thread on screen is called, for the strip a phone reads instead
  // of the sidebar: the server's title, or the first thing typed into a
  // sitting the sessions list has not heard of yet.
  const openTitle = useMemo(() => {
    const row = (sessions.data ?? []).find((s) => s.session_id === sessionId);
    return row?.title || thread[0]?.message || "";
  }, [sessions.data, sessionId, thread]);

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
      toast.success(t("automation.started", { count: String(howMany(r)) }));
      qc.invalidateQueries({ queryKey: ["automation-history", sessionId] });
      qc.invalidateQueries({ queryKey: ["automation-sessions"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  /** Forget a conversation. Not the work it started.
   *
   *  The confirm text carries the whole of that distinction, because "delete
   *  chat" reads as "cancel the documentation build" to anybody who has just
   *  approved one. What goes is the transcript: the sentences, the plans, and
   *  the record of which were pressed. What stays is every job those presses
   *  queued — they are the queue's, they are on the Job queue page, and they
   *  finish whether or not the thread that asked for them still exists.
   */
  const remove = useMutation({
    mutationFn: (sid: string) =>
      api<void>(`/api/automation/sessions/${encodeURIComponent(sid)}`,
                { method: "DELETE", token }),
    onSuccess: (_deleted, sid) => {
      toast.success(t("automation.chatDeleted"));
      qc.invalidateQueries({ queryKey: ["automation-sessions"] });
      // The rows are gone on the server; a cached copy of them is a thread
      // that would render instantly if anything asked for that id again.
      qc.removeQueries({ queryKey: ["automation-history", sid] });
      // The thread on screen is the one that went. A fresh sitting, rather
      // than a composer still posting into rows that no longer exist.
      if (sid === sessionId) startNewChat();
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const send = () => {
    const text = draft.trim();
    if (!text || reading || propose.isPending || !sessionId) return;
    setDraft("");
    propose.mutate(text);
  };

  const askToDelete = async (sid: string) => {
    const ok = await askFirst({
      title: t("automation.deleteChatTitle"),
      description: t("automation.deleteChatBody"),
      confirmLabel: t("automation.deleteChat"),
      danger: true,
    });
    if (ok) remove.mutate(sid);
  };

  return (
    <PageShell
      width="wide"
      // A chat is a column that fills the window, not a document that grows
      // one. With the page scrolling instead, every turn pushed the composer
      // further below the fold — on a phone the thing you type into was off
      // screen by the third question, and "scroll down to answer" is not a
      // conversation. The window is the height; only the transcript scrolls.
      className="flex h-[calc(100dvh_-_2.75rem_-_env(safe-area-inset-top))] min-h-0 flex-col gap-4 space-y-0"
    >
      <PageHeader
        icon={<WandIcon className="h-6 w-6" />}
        title={t("automation.title")}
        description={
          // Two sentences are five lines at 390px, and they sit between the
          // title and the thread. Clamped rather than dropped: an empty
          // thread opens with the capabilities message, which says the same
          // thing at more length and in the right place.
          <span className="line-clamp-2 sm:line-clamp-none">
            {t("automation.subtitle")}
          </span>
        }
        actions={
          <Button size="sm" variant="outline" onClick={startNewChat}>
            <PlusIcon className="mr-1 h-3.5 w-3.5" />
            {t("automation.newChat")}
          </Button>
        }
      />

      <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]">
        {/* Under lg the list is a sheet over the thread rather than a column
            beside it: two columns sharing 380px leave neither a sentence nor
            a title readable, and the thread is what was asked for. */}
        {sessionsOpen && (
          <div
            className="fixed inset-0 z-30 bg-black/60 lg:hidden"
            onClick={() => setSessionsOpen(false)}
            aria-hidden
          />
        )}

        <div
          className={cn(
            "fixed inset-y-0 left-0 z-40 flex w-[min(20rem,85vw)] min-h-0 flex-col",
            "bg-[var(--color-card)] shadow-[var(--shadow-lg)] transition-transform duration-200",
            // `invisible`, not just off-screen: a drawer nobody can see whose
            // buttons are still tabbable is a trap for a keyboard.
            sessionsOpen ? "translate-x-0" : "-translate-x-full invisible",
            // A plain grid column again as soon as there is room for both.
            "lg:visible lg:static lg:z-auto lg:w-auto lg:translate-x-0",
            "lg:bg-transparent lg:shadow-none lg:transition-none",
          )}
        >
          <Card className="flex min-h-0 min-w-0 flex-1 flex-col rounded-none border-0 lg:rounded-xl lg:border">
            <div className="flex shrink-0 items-start gap-2 border-b border-[var(--color-border)] p-3 lg:border-b-0 lg:pb-1">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">{t("automation.historyTitle")}</p>
                <p className="mt-0.5 text-[11px] leading-snug text-[var(--color-muted-foreground)]">
                  {t("automation.historyDesc")}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setSessionsOpen(false)}
                aria-label={t("automation.closeChats")}
                className="grid size-11 shrink-0 place-items-center rounded-md text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] lg:hidden"
              >
                <XIcon className="h-4 w-4" />
              </button>
            </div>

            <CardContent className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-2">
              {(sessions.data ?? []).length === 0 ? (
                <p className="px-2 py-3 text-xs text-[var(--color-muted-foreground)]">
                  {t("automation.historyEmpty")}
                </p>
              ) : (
                <ul className="space-y-1">
                  {(sessions.data ?? []).map((s) => (
                    <li key={s.session_id} className="group flex items-center gap-1">
                      <button
                        type="button"
                        onClick={() => selectSession(s.session_id)}
                        aria-current={s.session_id === sessionId ? "true" : undefined}
                        className={cn(
                          "min-w-0 flex-1 rounded-lg border px-2.5 py-2 text-left transition-colors",
                          s.session_id === sessionId
                            ? "border-[var(--color-brand)]/40 bg-[var(--color-brand-muted)]"
                            : "border-transparent hover:border-[var(--color-border)] hover:bg-[var(--color-muted)]/40",
                        )}
                      >
                        {/* The first sentence asked, which is the only title a
                            session can have that nobody had to invent. */}
                        <span className="block truncate text-[13px]">
                          {s.title || t("automation.sessionUntitled")}
                        </span>
                        <span className="mt-0.5 block truncate text-[11px] text-[var(--color-muted-foreground)]">
                          {t("automation.sessionRuns", { count: String(s.runs) })}
                          {" · "}
                          {s.last_at ? new Date(s.last_at).toLocaleString() : ""}
                        </span>
                      </button>
                      {/* Full-size and always there on touch, where there is
                          no hover to reveal it; a pointer gets it on hover or
                          on focus, so the keyboard never loses it either. */}
                      <button
                        type="button"
                        onClick={() => askToDelete(s.session_id)}
                        disabled={remove.isPending && remove.variables === s.session_id}
                        title={t("automation.deleteChat")}
                        aria-label={t("automation.deleteChat")}
                        className="grid size-11 shrink-0 place-items-center rounded-md text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-destructive)]/10 hover:text-[var(--color-destructive)] disabled:opacity-50 lg:size-8 lg:opacity-0 lg:group-hover:opacity-100 lg:group-focus-within:opacity-100"
                      >
                        {remove.isPending && remove.variables === s.session_id
                          ? <Loader2Icon className="h-4 w-4 animate-spin" />
                          : <Trash2Icon className="h-4 w-4" />}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>

        <Card className="flex min-h-0 min-w-0 flex-col">
          {/* The strip a phone gets instead of the sidebar: one press to the
              list, and the name of the thread you are in — which the sidebar
              was the only thing saying. */}
          <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-2 py-1.5 lg:hidden">
            <button
              type="button"
              aria-expanded={sessionsOpen}
              onClick={() => setSessionsOpen(true)}
              className="flex min-h-11 shrink-0 items-center gap-1.5 rounded-md px-2 text-xs font-medium transition-colors hover:bg-[var(--color-accent)]"
            >
              <ClockIcon className="h-4 w-4 text-[var(--color-muted-foreground)]" />
              {t("automation.chats")}
              {(sessions.data ?? []).length > 0 && (
                <Badge variant="outline" className="text-[10px]">
                  {(sessions.data ?? []).length}
                </Badge>
              )}
            </button>
            <span className="min-w-0 flex-1 truncate text-right text-xs text-[var(--color-muted-foreground)]">
              {openTitle}
            </span>
          </div>

          <div
            ref={scrollRef}
            className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain p-3 sm:p-6"
          >
            {thread.length === 0 && <Capabilities onPick={setDraft} />}

            {thread.map((h) => (
              <div key={h.id} className="space-y-2">
                {/* Verbatim. The plan is a reading of the sentence and can be
                    wrong; without the sentence beside it there is no way to
                    see that it was misread. */}
                <div className="flex justify-end">
                  <div className="max-w-[85%] whitespace-pre-wrap wrap-anywhere rounded-lg border border-[var(--color-brand)]/30 bg-[var(--color-brand)]/10 px-3 py-2 text-sm">
                    {h.message}
                  </div>
                </div>
                <Reply
                  run={h}
                  dismissed={dismissed.includes(h.id)}
                  stopping={stop.isPending && stop.variables === h.id}
                  starting={confirm.isPending && confirm.variables === h.id}
                  onStop={() => stop.mutate(h.id)}
                  onConfirm={() => confirm.mutate(h.id)}
                  onCancel={() => setDismissed((d) => [...d, h.id])}
                  onPick={setDraft}
                />
              </div>
            ))}
          </div>

          {/* The bottom of the column, so it is on screen at every length of
              thread. The safe-area padding is what keeps the send button off
              the home indicator, where a press lands on the wrong thing. */}
          <div className="shrink-0 border-t border-[var(--color-border)] bg-[var(--color-background)]/95 p-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] backdrop-blur sm:p-3">
            <div className="flex items-end gap-2">
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  // Enter sends; Shift+Enter is a newline. NOT on a touch
                  // keyboard, where Enter is the return key and there is no
                  // Shift to hold: a phone has a send button, and a sentence
                  // posted at the first line break buys a model call and a
                  // plan for half a thought.
                  const touch = window.matchMedia("(pointer: coarse)").matches;
                  if (e.key === "Enter" && !e.shiftKey && !touch) {
                    e.preventDefault();
                    send();
                  }
                }}
                rows={1}
                placeholder={t("automation.placeholder")}
                // Grows with the sentence up to four lines where the browser
                // supports it, instead of scrolling a one-line box.
                className="max-h-32 min-h-11 flex-1 resize-none rounded-md border border-[var(--color-input)] bg-transparent px-3 py-2.5 text-base field-sizing-content sm:min-h-9 sm:py-2 sm:text-sm"
              />
              <Button
                size="sm"
                className="h-11 px-4 sm:h-9 sm:px-3"
                disabled={!draft.trim() || reading || propose.isPending || !sessionId}
                onClick={send}
                title={t("automation.send")}
                aria-label={t("automation.send")}
              >
                {propose.isPending
                  ? <Loader2Icon className="h-4 w-4 animate-spin" />
                  : <SendIcon className="h-4 w-4" />}
              </Button>
            </div>
          </div>
        </Card>
      </div>
      {confirmDialog}
    </PageShell>
  );
}

/** What came back for one sentence: an answer, a plan awaiting approval, or a
 *  refusal. Rendered from the stored row, so it is the same on a reload. */
function Reply({
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
          <p className="whitespace-pre-wrap wrap-anywhere text-xs text-[var(--color-muted-foreground)]">
            {partial}
            {/* Says the sentence is not finished, which the spinner says
                about the reply and not about this line. Decoration only —
                a screen reader has `aria-busy` for the same fact. */}
            <span
              aria-hidden
              className="ml-0.5 inline-block h-3 w-0.5 align-middle animate-pulse rounded-[1px] bg-[var(--color-brand)]"
            />
          </p>
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
      {run.note && (
        <p className="whitespace-pre-wrap wrap-anywhere text-xs text-[var(--color-muted-foreground)]">
          {run.note}
        </p>
      )}

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
            <Badge variant="brand" className="text-[10px]">
              {said("automation.repoCount", { count: String(s.resolved_repos.length) })}
            </Badge>
            {Object.entries(s.arguments)
              .filter(([, v]) => v !== null && v !== undefined && v !== "")
              .filter(([k]) => k !== "repo_slugs" && k !== "owner")
              .map(([k, v]) => (
                <code key={k} className="rounded bg-[var(--color-muted)]/60 px-1.5 py-0.5 text-[11px]">
                  {k}: {String(v)}
                </code>
              ))}
          </div>
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

      {run.status === "started" && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
          <span>{said("automation.started", { count: String(howMany(run.result)) })}</span>
          {howMany(run.result) > 0 && (
            <Badge variant="brand" className="text-[10px]">
              {said("automation.queuedCount", { count: String(howMany(run.result)) })}
            </Badge>
          )}
        </div>
      )}

      {/* Below whatever the model said, never instead of it: its note is the
          only part that is about this particular sentence. In the language of
          that sentence too — this is the reply to a question nobody could
          read, and answering it in a third language helps nobody. */}
      {unread && <Capabilities onPick={onPick} language={run.language} />}
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
function Capabilities({
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
