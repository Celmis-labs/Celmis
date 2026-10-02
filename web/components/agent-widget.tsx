"use client";

/**
 * The Celmis agent, one press away on every page.
 *
 * It used to be a row in the sidebar that led to a page of its own, which put
 * the one thing that can answer "where do I set this up?" exactly where
 * somebody who is lost would not look — and took them away from the page they
 * had the question about. Now it is a round button in the corner, and the
 * conversation opens over whatever is on screen.
 *
 * THE SAME CONVERSATION AS /automation, NOT A SECOND ONE. The thread, the
 * session id, the polling and the gate that keeps work from running on the
 * first press are `useAutomationThread` from components/automation/thread.tsx
 * — the hook the full page uses — so a question asked here is the newest turn
 * there, and "open full view" continues it rather than starting over.
 *
 * Links in an answer are router links, and this panel lives in the shell, so
 * following one changes the page underneath and leaves the panel open: the
 * answer stays readable next to the screen it was describing.
 *
 * Nothing is fetched until the panel is opened. The thread hook lives in the
 * panel body, which is not mounted while the button is just a button — every
 * page in the app should not pay for a chat history nobody asked to see.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useSyncExternalStore } from "react";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import {
  Loader2Icon, Maximize2Icon, PlusIcon, SendIcon, SparklesIcon, XIcon,
} from "lucide-react";

import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import {
  Reply, newSessionId, sendsOnEnter, useAutomationThread,
} from "@/components/automation/thread";
import { Button } from "@/components/ui/button";

/** Open or closed survives navigation and reloads: somebody following a link
 *  out of an answer, or reloading the page it led to, comes back to the
 *  panel they had open. A tiny external store rather than state in the shell
 *  because both the button and the panel's own close control write it. */
const OPEN_KEY = "celmis:agent-widget";
const openListeners = new Set<() => void>();

function subscribeOpen(onChange: () => void): () => void {
  openListeners.add(onChange);
  return () => { openListeners.delete(onChange); };
}

function readOpen(): boolean {
  try {
    return localStorage.getItem(OPEN_KEY) === "open";
  } catch {
    return false;
  }
}

function setOpen(open: boolean): void {
  try {
    localStorage.setItem(OPEN_KEY, open ? "open" : "closed");
  } catch {
    /* private mode — the panel still opens, it just will not be remembered */
  }
  openListeners.forEach((fn) => fn());
}

/** Where the button would sit on top of something that is already there.
 *
 *  /automation is the full view of this same conversation. The Q&A chat and
 *  a Claude session both put their own composer, with its send button, in
 *  exactly the bottom-right corner this button occupies — a round button over
 *  a send button is a mis-tap waiting to happen, and on those screens there
 *  is already a conversation in front of the reader. */
const HIDDEN_ON = [
  /^\/automation(\/|$)/,
  /^\/projects\/[^/]+\/chats\/[^/]+/,
  /^\/claude\/[^/]+/,
];

/** Questions a newcomer actually has, one press into the composer. Setup and
 *  where-is questions first: they are what this panel is for on a page that
 *  is not the agent's own. */
const SUGGESTIONS = [
  "agentWidget.q.gitlab",
  "agentWidget.q.github",
  "agentWidget.q.reviewPrompt",
  "agentWidget.q.llmKey",
  "agentWidget.q.repos",
] as const;

/** Spring for the panel: quick to arrive, settles without a wobble. */
const PANEL_SPRING = { type: "spring", stiffness: 420, damping: 34, mass: 0.8 } as const;

export function AgentWidget() {
  const t = useT();
  const token = useToken();
  const pathname = usePathname();
  const open = useSyncExternalStore(subscribeOpen, readOpen, () => false);
  const hidden = HIDDEN_ON.some((re) => re.test(pathname));

  // Escape closes, from anywhere — the panel is not modal, so focus may well
  // be on the page beneath it.
  useEffect(() => {
    if (!open || hidden) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, hidden]);

  if (!token || hidden) return null;

  return (
    <>
      <AnimatePresence>
        {open && <AgentPanel key="agent-panel" />}
      </AnimatePresence>

      {/* Bottom-right, clear of the home indicator and the notch's side
          insets. Toasts are at the top-right, so the two never meet. z-[25]:
          above the page and its sticky bar (z-10), below the phone drawer's
          backdrop (z-30) so opening the menu dims it like everything else. */}
      <m.button
        type="button"
        onClick={() => setOpen(!open)}
        aria-label={open ? t("agentWidget.close") : t("agentWidget.open")}
        title={open ? t("agentWidget.close") : t("agentWidget.open")}
        aria-expanded={open}
        aria-controls="agent-panel"
        initial={{ opacity: 0, scale: 0.6 }}
        animate={{ opacity: 1, scale: 1 }}
        whileHover={{ scale: 1.06 }}
        whileTap={{ scale: 0.94 }}
        transition={{ type: "spring", stiffness: 500, damping: 30 }}
        className="print:hidden fixed bottom-[max(1rem,calc(env(safe-area-inset-bottom)+0.75rem))] right-[max(1rem,calc(env(safe-area-inset-right)+0.75rem))] z-[25] grid size-12 place-items-center rounded-full bg-[var(--color-brand)] text-[var(--color-brand-foreground)] shadow-[var(--shadow-lg)] ring-1 ring-black/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-background)]"
      >
        <AnimatePresence mode="wait" initial={false}>
          <m.span
            key={open ? "x" : "spark"}
            initial={{ opacity: 0, rotate: -45, scale: 0.6 }}
            animate={{ opacity: 1, rotate: 0, scale: 1 }}
            exit={{ opacity: 0, rotate: 45, scale: 0.6 }}
            transition={{ duration: 0.14 }}
            className="grid place-items-center"
          >
            {open ? <XIcon className="h-5 w-5" /> : <SparklesIcon className="h-5 w-5" />}
          </m.span>
        </AnimatePresence>
      </m.button>
    </>
  );
}

function AgentPanel() {
  const t = useT();
  const composer = useRef<HTMLTextAreaElement>(null);
  const {
    sessionId, selectSession, thread, reading, draft, setDraft, send,
    dismissed, setDismissed, scrollRef, propose, stop, confirm,
  } = useAutomationThread();

  // Focus the composer when the panel opens — with a mouse. On a phone that
  // would throw up the keyboard over the answer the person opened it to read.
  useEffect(() => {
    if (window.matchMedia("(pointer: fine)").matches) composer.current?.focus();
  }, []);

  const pick = (text: string) => {
    setDraft(text);
    composer.current?.focus();
  };

  // The last few turns. The full view has the whole thread and every older
  // one; a panel this size showing fifty turns is a scrollbar, not a chat.
  const recent = thread.slice(-20);

  return (
    <m.section
      id="agent-panel"
      role="dialog"
      aria-label={t("agentWidget.title")}
      initial={{ opacity: 0, y: 16, scale: 0.94 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: 10, scale: 0.97, transition: { duration: 0.14 } }}
      transition={PANEL_SPRING}
      style={{ transformOrigin: "100% 100%" }}
      className="print:hidden fixed inset-x-2 bottom-[calc(max(1rem,calc(env(safe-area-inset-bottom)+0.75rem))+3.75rem)] z-[25] flex h-[min(40rem,calc(100dvh-env(safe-area-inset-top)-9rem))] flex-col overflow-hidden rounded-2xl border border-[var(--color-border)] bg-[var(--color-card)] shadow-[var(--shadow-lg)] sm:left-auto sm:right-[max(1rem,calc(env(safe-area-inset-right)+0.75rem))] sm:w-[26rem]"
    >
      <header className="flex shrink-0 items-center gap-2.5 border-b border-[var(--color-border)] bg-[var(--color-muted)]/40 px-3 py-2.5">
        <span className="grid size-8 shrink-0 place-items-center rounded-full bg-[var(--color-brand)] text-[var(--color-brand-foreground)]">
          <SparklesIcon className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold leading-tight">{t("agentWidget.title")}</p>
          <p className="truncate text-[11px] text-[var(--color-muted-foreground)]">
            {t("agentWidget.subtitle")}
          </p>
        </div>
        <button
          type="button"
          onClick={() => selectSession(newSessionId())}
          title={t("agentWidget.newChat")}
          aria-label={t("agentWidget.newChat")}
          className="grid size-9 place-items-center rounded-md text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)] sm:size-8"
        >
          <PlusIcon className="h-4 w-4" />
        </button>
        <Link
          href="/automation"
          onClick={() => setOpen(false)}
          title={t("agentWidget.fullView")}
          aria-label={t("agentWidget.fullView")}
          className="grid size-9 place-items-center rounded-md text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)] sm:size-8"
        >
          <Maximize2Icon className="h-4 w-4" />
        </Link>
        <button
          type="button"
          onClick={() => setOpen(false)}
          title={t("agentWidget.close")}
          aria-label={t("agentWidget.close")}
          className="grid size-9 place-items-center rounded-md text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)] sm:size-8"
        >
          <XIcon className="h-4 w-4" />
        </button>
      </header>

      <div
        ref={scrollRef}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain p-3"
        aria-live="polite"
      >
        {recent.length === 0 ? (
          <div className="space-y-3 px-1 py-2">
            <p className="text-sm leading-relaxed text-[var(--color-muted-foreground)]">
              {t("agentWidget.intro")}
            </p>
            <div>
              <p className="mb-1.5 text-xs font-medium">{t("agentWidget.suggested")}</p>
              {/* Into the composer, not straight to the agent: the same rule
                  as the full page's examples — the one sentence that is sent
                  is the one somebody pressed Send on. */}
              <div className="flex flex-wrap gap-1.5">
                {SUGGESTIONS.map((key, i) => (
                  <m.button
                    key={key}
                    type="button"
                    onClick={() => pick(t(key))}
                    initial={{ opacity: 0, y: 4 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.05 + i * 0.03, duration: 0.18 }}
                    className="rounded-full border border-[var(--color-border)] bg-[var(--color-background)] px-3 py-1.5 text-left text-xs transition-colors hover:border-[var(--color-brand)]/40 hover:bg-[var(--color-accent)] hover:text-[var(--color-brand)]"
                  >
                    {t(key)}
                  </m.button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          recent.map((h) => (
            <div key={h.id} className="space-y-2">
              <div className="flex justify-end">
                <div className="max-w-[85%] whitespace-pre-wrap wrap-anywhere rounded-2xl rounded-br-md bg-[var(--color-brand)] px-3 py-2 text-sm text-[var(--color-brand-foreground)]">
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
                onPick={pick}
              />
            </div>
          ))
        )}
      </div>

      <div className="shrink-0 border-t border-[var(--color-border)] p-2">
        <div className="flex items-end gap-2 rounded-xl border border-[var(--color-input)] bg-[var(--color-background)] p-1 pl-2.5 transition-colors focus-within:border-[var(--color-ring)]">
          <textarea
            ref={composer}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (sendsOnEnter(e)) {
                e.preventDefault();
                send();
              }
            }}
            rows={1}
            placeholder={t("agentWidget.placeholder")}
            aria-label={t("agentWidget.placeholder")}
            className="max-h-28 min-h-9 flex-1 resize-none bg-transparent py-2 text-base outline-none field-sizing-content placeholder:text-[var(--color-muted-foreground)] sm:text-sm"
          />
          <Button
            size="icon"
            className="size-9 shrink-0 rounded-lg sm:size-8"
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
    </m.section>
  );
}
