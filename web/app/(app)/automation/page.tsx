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
 *
 * THE CONVERSATION ITSELF IS SHARED. The thread hook and the reply cards live
 * in components/automation/thread.tsx, because the same conversation is also
 * a panel floating over every other page. This file is what only the full
 * view has: the list of previous chats and deleting one.
 */

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ClockIcon, Loader2Icon, PlusIcon, SendIcon, Trash2Icon, WandIcon, XIcon,
} from "lucide-react";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";
import {
  Capabilities, Reply, newSessionId, sendsOnEnter, useAutomationThread,
} from "@/components/automation/thread";
import { PageHeader, PageShell } from "@/components/page-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";

export default function AutomationPage() {
  const t = useT();
  const qc = useQueryClient();
  // Aliased on purpose: `confirm` on this page is already the mutation that
  // RUNS a plan. A `confirm` that asks and a `confirm` that starts an hour of
  // model time, declared four lines apart, is the one name collision in this
  // file worth spending a rename on.
  const { confirm: askFirst, dialog: confirmDialog } = useConfirm();

  // The thread itself — session, history, polling, ask / stop / run — is the
  // same hook the floating panel uses. What is left here is what only a
  // full page has: the list of previous conversations, and deleting one.
  const {
    token, sessionId, selectSession: openSession, sessions, thread, reading,
    draft, setDraft, send, dismissed, setDismissed, scrollRef,
    propose, stop, confirm,
  } = useAutomationThread();
  const [sessionsOpen, setSessionsOpen] = useState(false);

  const selectSession = (sid: string) => {
    openSession(sid);
    setSessionsOpen(false);
  };

  const startNewChat = () => {
    const sid = newSessionId();
    selectSession(sid);
  };

  // What the thread on screen is called, for the strip a phone reads instead
  // of the sidebar: the server's title, or the first thing typed into a
  // sitting the sessions list has not heard of yet.
  const openTitle = useMemo(() => {
    const row = (sessions.data ?? []).find((s) => s.session_id === sessionId);
    return row?.title || thread[0]?.message || "";
  }, [sessions.data, sessionId, thread]);

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
                  // Enter sends, except on a touch keyboard — see sendsOnEnter.
                  if (sendsOnEnter(e)) {
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
