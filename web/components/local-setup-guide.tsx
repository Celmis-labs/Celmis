"use client";

/**
 * How to stand a local model server up, in the two places that answer it.
 *
 * The content is authored on the backend, in English, so the commands can
 * track new server projects without a frontend release — see
 * GET /api/llm/local-setup-guide. This file is only how it is shown.
 *
 * One component, two surfaces. /settings/llm shows it as a disclosure beside
 * every provider select; the Celmis agent shows the same commands under its
 * answer to "can I run this on my own models". They were never allowed to
 * drift into two renderings of one document, so they are one.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BookOpenIcon, CheckIcon, ChevronDownIcon, CopyIcon, RefreshCwIcon,
} from "lucide-react";

import { llmApi, type LocalSetupGuide } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { Button } from "@/components/ui/button";

/** A `t()`. Passed in rather than taken from the hook because the two callers
 *  read from different dictionaries: the settings page uses the interface
 *  language, a chat reply the language of the question. */
type Translate = (key: string, vars?: Record<string, string | number>) => string;

/** Same shape as the webhook page's Copyable — a command you paste into a
 *  terminal, with the one-click copy that pattern already taught users.
 *  Long commands scroll inside the code box so mobile never scrolls the page. */
export function CopyableCode({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2">
      <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre rounded-md border border-[var(--color-border)] bg-[var(--color-muted)]/30 px-2 py-1.5 text-xs">
        {value}
      </code>
      <Button
        size="sm" variant="outline" className="h-8 shrink-0"
        title={label} aria-label={label}
        onClick={() => {
          void navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
      >
        {copied ? <CheckIcon className="h-3.5 w-3.5" /> : <CopyIcon className="h-3.5 w-3.5" />}
      </Button>
    </div>
  );
}

/**
 * The guide itself: what to start, and the line that starts it.
 *
 * The commands are rendered exactly as the server wrote them. `ollama serve`
 * is `ollama serve` in every language and a translated flag is a broken one,
 * so nothing inside a code box goes near a dictionary. Only the labels around
 * them do.
 *
 * `commandsOnly` drops the guide's own English PROSE — the per-option notes
 * and the re-index warning. On a settings card that prose sits among other
 * English-authored server text and reads as documentation; under a reply
 * written in Japanese it reads as a bug, and the paragraph it would have
 * contributed is already written down there in sixteen languages.
 */
export function LocalSetupGuideBody({
  guide, t, commandsOnly = false,
}: {
  guide: LocalSetupGuide;
  t: Translate;
  commandsOnly?: boolean;
}) {
  return (
    <>
      {guide.options.map((o) => (
        <div key={o.name} className="space-y-1.5">
          <div className="text-sm font-medium">{o.name}</div>
          <CopyableCode value={o.command} label={t("settings.llm.copy")} />
          {o.base_url_hint && (
            <div className="text-xs text-[var(--color-muted-foreground)]">
              {t("settings.llm.selfHostedBaseUrlLabel")}: <code>{o.base_url_hint}</code>
            </div>
          )}
          {!commandsOnly && o.notes && (
            <p className="text-xs text-[var(--color-muted-foreground)]">{o.notes}</p>
          )}
        </div>
      ))}
      {guide.env.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-sm font-medium">{t("settings.llm.guideEnv")}</div>
          {guide.env.map((line, i) => (
            <CopyableCode key={i} value={line} label={t("settings.llm.copy")} />
          ))}
        </div>
      )}
      {!commandsOnly && guide.reindex_warning && (
        <div className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          <RefreshCwIcon className="h-3.5 w-3.5 shrink-0" /> {guide.reindex_warning}
        </div>
      )}
    </>
  );
}

/**
 * The one line beside a provider select that says local models exist, and the
 * disclosure that shows how to connect one.
 *
 * It is a line and a chevron because it is permanent: it sits on all four
 * surface cards of a page that is already dense, and four expanded panels
 * would be a manual where a hint belongs. Collapsed it costs one row;
 * expanded it is the whole guide.
 *
 * Fetched on first open and cached for the session — most visits never expand
 * it, and the ones that do usually expand it once.
 *
 * `hint` is the caller's, because the sentence differs by what the reader can
 * actually do: on chat, review and the agent the provider is in the dropdown
 * right there, and for embeddings the choice is the operator's, in the server
 * environment.
 *
 * `defaultOpen` is only ever the FIRST state, as its name says. A caller that
 * needs a later change to reopen it gives the panel a different `key` and
 * gets a fresh one; nothing is lost, because the guide itself is cached.
 */
export function LocalSetupGuidePanel({
  hint, defaultOpen = false,
}: {
  hint: string;
  defaultOpen?: boolean;
}) {
  const token = useToken();
  const t = useT();
  const [open, setOpen] = useState(defaultOpen);
  const guide = useQuery({
    queryKey: ["llm-local-setup-guide"],
    queryFn: () => llmApi.localSetupGuide(token!),
    enabled: !!token && open,
    staleTime: Infinity,
  });
  return (
    <div className="rounded-lg border border-[var(--color-border)]">
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
        className="flex w-full items-start justify-between gap-3 px-3 py-2 text-left text-xs sm:items-center">
        <span className="flex min-w-0 items-start gap-2 text-[var(--color-muted-foreground)] sm:items-center">
          <BookOpenIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 opacity-60 sm:mt-0" />
          <span className="min-w-0">{hint}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1">
          <span className="hidden underline decoration-dotted underline-offset-2 sm:inline">
            {t("settings.llm.guideToggle")}
          </span>
          <ChevronDownIcon className={cn("h-4 w-4 shrink-0 opacity-60 transition-transform", open && "rotate-180")} />
        </span>
      </button>
      {open && (
        <div className="space-y-4 border-t border-[var(--color-border)] px-3 py-3">
          {guide.isLoading && (
            <div className="text-xs text-[var(--color-muted-foreground)]">{t("settings.llm.guideLoading")}</div>
          )}
          {guide.error ? (
            <div className="whitespace-pre-wrap text-xs text-red-700 dark:text-red-400">
              {(guide.error as Error).message}
            </div>
          ) : null}
          {guide.data && <LocalSetupGuideBody guide={guide.data} t={t} />}
        </div>
      )}
    </div>
  );
}
