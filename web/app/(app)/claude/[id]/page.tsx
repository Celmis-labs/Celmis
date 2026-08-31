"use client";

/**
 * /claude/{id} — live view of one agent session.
 *
 * Replays the persisted event log and tails live events over SSE. The
 * session runs server-side: closing this page changes nothing, reopening
 * replays everything (cursor = last event id).
 */

import Link from "next/link";
import { memo, use, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  PauseIcon,
  ArrowLeftIcon, BotIcon, CheckCircle2Icon, CheckIcon, ExternalLinkIcon,
  GitBranchIcon, Loader2Icon, PaperclipIcon, SendIcon, SquareIcon, WrenchIcon,
  XCircleIcon,
} from "lucide-react";

import { claudeApi } from "@/lib/api";
import { useSSE } from "@/lib/use-sse";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Markdown } from "@/components/markdown";
import { PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

type FeedItem =
  | { kind: "text"; text: string }
  | { kind: "tool"; name: string; input: string; error?: boolean }
  | { kind: "note"; text: string }
  // The user's own turns. Without this the transcript showed only the agent's
  // half of a conversation, and the opening prompt appeared solely as the h1.
  | { kind: "user"; text: string };

/**
 * Only the assistant's prose is markdown — tool rows, tool-result previews and
 * notes are verbatim payloads, so they keep rendering literally.
 *
 * Memoised because every SSE frame appends to `feed` and re-renders the whole
 * list: without it each frame would re-parse every earlier answer, which is
 * felt on a phone once a session has run for a while.
 *
 * Scope also contains damage. runner.py emits one event per completed
 * TextBlock, so a fence is never split across items; and even if one arrived
 * half-open, remark closes it at the end of *this* item's input, so the worst
 * case is one row shown as code rather than the rest of the feed vanishing.
 *
 * wrap-anywhere is this page's rule for paths/SHAs/URLs. Tailwind emits it
 * after the component's own break-words, so it wins the cascade and no single
 * long token in prose can widen the document past 390px.
 */
const AssistantText = memo(function AssistantText({ text }: { text: string }) {
  return <Markdown text={text} className="wrap-anywhere" />;
});

export default function AgentSessionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const sse = useSSE();
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);

  // The turn is echoed locally as well as arriving back over the stream: the
  // round trip is a queue put plus a model call, and an input that clears with
  // nothing to show for it reads as a lost message.
  const sendDraft = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      await claudeApi.sendMessage(token!, id, text);
      setDraft("");
      // The stream stopped retrying when the session paused — deliberately,
      // because nothing was coming. This turn is what makes something come.
      wakeRef.current?.();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSending(false);
    }
  };
  const [retrying, setRetrying] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Stream state. The tail survives sleep/lock/tab-switch by reconnecting
  // from lastIdRef (?after=), so nothing between the drop and the wake is lost.
  const lastIdRef = useRef(0);
  const doneRef = useRef(false);
  const inFlightRef = useRef(false);
  const deadRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const lastFrameRef = useRef(0);
  // Installed by the stream effect; called after a message is sent, because a
  // paused session's stream is closed and only the person typing reopens it.
  const wakeRef = useRef<(() => void) | null>(null);

  const session = useQuery({
    queryKey: ["agent-session", id],
    queryFn: () => claudeApi.session(token!, id),
    enabled: !!token,
    refetchInterval: (q) =>
      q.state.data?.status === "running" || q.state.data?.status === "queued" ? 4000 : false,
  });

  useEffect(() => {
    if (!token) return;
    deadRef.current = false;

    const clearTimer = () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = null;
    };

    const retryLater = () => {
      if (deadRef.current || doneRef.current || timerRef.current) return;
      const delay = Math.min(2000 * 2 ** attemptRef.current++, 15000);
      setRetrying(true);
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        void connect();
      }, delay);
    };

    const connect = async () => {
      if (deadRef.current || doneRef.current || inFlightRef.current) return;
      inFlightRef.current = true;
      lastFrameRef.current = Date.now();
      try {
        await sse.send({
          url: claudeApi.streamUrl(id, lastIdRef.current),
          token,
          method: "GET",
          onEvent: (ev, raw, evId) => {
            const n = Number(evId);
            if (Number.isFinite(n) && n > lastIdRef.current) lastIdRef.current = n;
            // The backoff resets on PROGRESS, not on arrival. `stream_end`
            // arrives on every reconnect that finds nothing — so counting it
            // reset the attempt counter each time and the delay never grew
            // past its 2s floor. Measured on a paused session: 36 connections
            // in 75 seconds, one every two seconds, for as long as the tab
            // stayed open.
            if (ev !== "stream_end") attemptRef.current = 0;
            lastFrameRef.current = Date.now();
            setRetrying((r) => (r ? false : r));
            const data = (raw ?? {}) as Record<string, unknown>;
            if (ev === "text" && typeof data.text === "string") {
              setFeed((f) => [...f, { kind: "text", text: data.text as string }]);
            } else if (ev === "tool_use") {
              setFeed((f) => [...f, {
                kind: "tool",
                name: String(data.name ?? "tool"),
                input: String(data.input ?? ""),
              }]);
            } else if (ev === "tool_result" && data.is_error) {
              setFeed((f) => [...f, {
                kind: "tool", name: "error", input: String(data.preview ?? ""), error: true,
              }]);
            } else if (ev === "meta") {
              const note = data.note ?? data.status;
              if (note) setFeed((f) => [...f, { kind: "note", text: String(note) }]);
            } else if (ev === "user") {
              setFeed((f) => [...f, { kind: "user", text: String(data.text ?? "") }]);
            } else if (ev === "pushed") {
              setFeed((f) => [...f, {
                kind: "note",
                text: `→ ${data.branch ?? "branch pushed"}`,
              }]);
            } else if (ev === "error") {
              setFeed((f) => [...f, { kind: "note", text: `⚠ ${data.detail ?? "error"}` }]);
            }
            // Terminal — the session is over, so stop reconnecting.
            //
            // `stream_end` is NOT one of those on its own: the server sends it
            // whenever this connection has no live queue, which includes an
            // API restart while the session is still running. Treating it as
            // terminal left the page permanently dead after every deploy. It
            // now carries the session's own status, and only that decides.
            // STOP RETRYING WHEN NOTHING IS COMING. `final` means the
            // session cannot continue at all; `resumable` means it continues
            // only when somebody types. Neither will produce another frame on
            // its own, and the retry loop treated only the first as a reason
            // to stop — so a paused session reconnected every fifteen seconds
            // for as long as the tab stayed open, showing an amber
            // "Reconnecting…" badge over a conversation that was perfectly
            // healthy and simply waiting.
            //
            // What must KEEP retrying is the third case: `stream_end` with
            // neither flag, which is a running session whose API restarted
            // under it. That is the deploy case the server comment describes,
            // and it is why this cannot just stop on every stream_end.
            const stopped =
              ev === "done" || ev === "error" ||
              (ev === "stream_end" &&
                (data.final === true || data.resumable === true));
            const finished =
              ev === "done" || ev === "error" ||
              (ev === "stream_end" && data.final === true);
            if (stopped) {
              doneRef.current = true;
              setRetrying(false);
            }
            if (finished) {
              doneRef.current = true;
              void qc.invalidateQueries({ queryKey: ["agent-session", id] });
              void qc.invalidateQueries({ queryKey: ["agent-sessions"] });
            }
          },
        });
      } finally {
        inFlightRef.current = false;
      }
      // send() resolves on a dropped connection just as it does on a clean
      // end, so only a terminal event above stops the retry loop.
      retryLater();
    };

    void connect();

    // A phone kills background sockets: reconnect the moment the page or the
    // network comes back, without waiting out the backoff.
    const wake = () => {
      if (deadRef.current || doneRef.current) return;
      // A socket dropped while backgrounded often never errors — the read just
      // hangs. The server pings every 15s, so silence past 30s means zombie:
      // abort it and let the send()'s retryLater bring the stream back.
      if (inFlightRef.current) {
        if (Date.now() - lastFrameRef.current > 30000) sse.cancel();
        return;
      }
      clearTimer();
      attemptRef.current = 0;
      void connect();
    };
    wakeRef.current = () => {
      doneRef.current = false;
      wake();
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") wake();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", wake);

    return () => {
      deadRef.current = true;
      wakeRef.current = null;
      clearTimer();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", wake);
      sse.cancel();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, id]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Follow the tail only when the reader is already at it — otherwise every
    // frame would yank them off whatever they scrolled back up to read.
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) el.scrollTop = el.scrollHeight;
  }, [feed.length]);

  const s = session.data;
  const live = s?.status === "running" || s?.status === "queued";
  // Paused is not finished. There is no process behind it, so the stream is
  // closed and the Stop button is meaningless — but the composer stays,
  // because sending a message is exactly what wakes it up. Showing this as a
  // dead session was the whole reason a conversation felt one-shot.
  const resumable = s?.status === "paused";

  return (
    <PageShell width="wide" className="space-y-4">
      <SectionTabs set="agent" />
      {/* Sticky: Stop has to stay one thumb away however long the log gets. */}
      <div className="sticky top-[calc(2.75rem+env(safe-area-inset-top))] z-20 -mx-4 flex items-center justify-between gap-2 border-b border-[var(--color-border)] bg-[var(--color-background)]/95 px-4 py-2 backdrop-blur sm:-mx-8 sm:px-8">
        <Link href="/claude" className="-ml-2 flex items-center gap-1 rounded-md px-2 py-3 text-sm text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]">
          <ArrowLeftIcon className="h-4 w-4" /> {t("claude.backToSessions")}
        </Link>
        {live && (
          <div className="flex items-center gap-2">
            {/* Finish is the ordinary end of a conversation: no more turns,
                then push and open the PR. Stop stays the abrupt one. */}
            <Button
              variant="outline" size="sm" className="h-9 px-3 sm:h-8"
              onClick={async () => {
                try {
                  await claudeApi.finish(token!, id);
                  toast.info(t("claude.finishing"));
                } catch (e) {
                  toast.error((e as Error).message);
                }
              }}
            >
              <CheckIcon className="mr-1 h-3.5 w-3.5" /> {t("claude.finish")}
            </Button>
            <Button
              variant="destructive" size="sm" className="h-9 px-3 sm:h-8"
              onClick={async () => {
                try {
                  await claudeApi.stop(token!, id);
                  toast.info(t("claude.stopping"));
                } catch (e) {
                  toast.error((e as Error).message);
                }
              }}
            >
              <SquareIcon className="mr-1 h-3.5 w-3.5" /> {t("claude.stop")}
            </Button>
          </div>
        )}
      </div>

      {s && (
        <div className="space-y-1">
          <h1 className="flex items-center gap-2 text-lg font-semibold">
            <BotIcon className="h-5 w-5" />
            <span className="truncate">{s.title || s.prompt}</span>
          </h1>
          <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted-foreground)]">
            <span>{s.repo_slug}</span>
            {s.status === "done" && <Badge variant="success"><CheckCircle2Icon className="mr-1 h-3 w-3" /> done</Badge>}
            {s.status === "error" && <Badge variant="destructive"><XCircleIcon className="mr-1 h-3 w-3" /> error</Badge>}
            {s.status === "cancelled" && <Badge>cancelled</Badge>}
            {s.status === "paused" && (
              <Badge variant="brand">
                <PauseIcon className="mr-1 h-3 w-3" /> {t("claude.pausedBadge")}
              </Badge>
            )}
            {live && <Badge variant="brand"><Loader2Icon className="mr-1 h-3 w-3 animate-spin" /> {s.status}</Badge>}
            {retrying && <Badge variant="warning">{t("claude.reconnecting")}</Badge>}
          </div>
        </div>
      )}

      <Card>
        <CardContent ref={scrollRef} className="max-h-[60dvh] space-y-3 overflow-y-auto overscroll-contain px-4 py-4 sm:px-6">
          {feed.length === 0 && (
            <div className="flex items-center gap-2 py-6 text-sm text-[var(--color-muted-foreground)]">
              {live || !s ? (
                <>
                  <Loader2Icon className="h-4 w-4 animate-spin" /> {t("claude.waiting")}
                </>
              ) : (
                t("claude.noEvents")
              )}
            </div>
          )}
          {feed.map((item, i) =>
            item.kind === "text" ? (
              <AssistantText key={i} text={item.text} />
            ) : item.kind === "user" ? (
              // Right-aligned and boxed, because a transcript where both
              // sides look identical is not a transcript.
              <div key={i} className="flex justify-end">
                <div className="max-w-[85%] whitespace-pre-wrap wrap-anywhere rounded-lg border border-[var(--color-brand)]/30 bg-[var(--color-brand)]/10 px-3 py-2 text-sm">
                  {item.text}
                </div>
              </div>
            ) : item.kind === "tool" ? (
              <div
                key={i}
                className={`flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-[13px] sm:text-xs ${
                  item.error
                    ? "border-red-500/40 bg-red-500/5 text-red-500"
                    : "border-[var(--color-border)] bg-[var(--color-muted)]/30 text-[var(--color-muted-foreground)]"
                }`}
              >
                <WrenchIcon className="mt-0.5 h-3 w-3 shrink-0" />
                <div className="min-w-0">
                  <span className="font-medium">{item.name}</span>{" "}
                  <span className="wrap-anywhere opacity-80">{item.input.slice(0, 200)}</span>
                </div>
              </div>
            ) : (
              <div key={i} className="text-[13px] italic text-[var(--color-muted-foreground)] sm:text-xs">
                {item.text}
              </div>
            ),
          )}
        </CardContent>
        {(live || resumable) && (
          // The whole point of the change: a session is a conversation, so
          // there has to be somewhere to say the next thing. Sticky at the
          // bottom, because the log grows and the input must not walk away.
          <div className="sticky bottom-0 border-t border-[var(--color-border)] bg-[var(--color-background)]/95 p-3 backdrop-blur">
            <div className="flex items-end gap-2">
              <label
                className="flex h-11 cursor-pointer items-center rounded-md border border-[var(--color-border)] px-3 text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)] sm:h-9"
                title={t("claude.attachHint")}
                aria-label={t("claude.attachHint")}
              >
                <PaperclipIcon className="h-4 w-4" />
                <input
                  type="file"
                  className="hidden"
                  accept=".txt,.log,.md,.csv,.tsv,.json,.yaml,.yml,.diff,.patch,.sql,.xml,.ini,.toml,.conf"
                  onChange={async (e) => {
                    const f = e.target.files?.[0];
                    e.target.value = "";
                    if (!f) return;
                    setSending(true);
                    try {
                      const r = await claudeApi.attach(token!, id, f);
                      toast.success(t("claude.attached", { path: r.path }));
                    } catch (err) {
                      toast.error((err as Error).message);
                    } finally {
                      setSending(false);
                    }
                  }}
                />
              </label>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  // Enter sends; Shift+Enter is a newline. A multi-line
                  // instruction is common enough that the reverse would be
                  // wrong.
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void sendDraft();
                  }
                }}
                rows={1}
                placeholder={t("claude.composerPlaceholder")}
                className="min-h-11 flex-1 resize-none rounded-md border border-[var(--color-input)] bg-transparent px-3 py-2.5 text-sm sm:min-h-9 sm:py-2"
              />
              <Button
                size="sm"
                className="h-11 px-3 sm:h-9"
                disabled={sending || !draft.trim()}
                onClick={() => void sendDraft()}
                title={t("claude.send")}
                aria-label={t("claude.send")}
              >
                {sending
                  ? <Loader2Icon className="h-4 w-4 animate-spin" />
                  : <SendIcon className="h-4 w-4" />}
              </Button>
            </div>
          </div>
        )}
      </Card>

      {s?.result?.branch && (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-3 py-4 text-sm">
            <GitBranchIcon className="h-4 w-4" />
            <code className="text-xs">{s.result.branch}</code>
            {s.result.pr_url ? (
              <a
                href={s.result.pr_url} target="_blank" rel="noreferrer"
                className="flex min-h-11 items-center gap-1 py-2 font-medium text-[var(--color-brand)] underline"
              >
                {t("claude.openPr")} <ExternalLinkIcon className="h-3.5 w-3.5" />
              </a>
            ) : s.result.compare_url && (
              <a
                href={s.result.compare_url} target="_blank" rel="noreferrer"
                className="flex min-h-11 items-center gap-1 py-2 text-[var(--color-brand)] underline"
              >
                {t("claude.openPr")} <ExternalLinkIcon className="h-3.5 w-3.5" />
              </a>
            )}
          </CardContent>
        </Card>
      )}

      {s?.error && (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-500">
          {s.error}
        </div>
      )}
    </PageShell>
  );
}
