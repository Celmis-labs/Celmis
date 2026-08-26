"use client";

import { use, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import {
  ArrowLeftIcon, SendIcon, FileTextIcon, LoaderIcon, TrashIcon, StopCircleIcon,
  CodeIcon, EyeOffIcon, ShieldAlertIcon, CheckIcon, XIcon,
} from "lucide-react";
import { toast } from "sonner";

import {
  askUrl, chatsApi,
  type ChatOut, type MessageMeta, type MessageOut,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useSSE } from "@/lib/use-sse";
import { useT } from "@/lib/i18n";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Card, CardContent } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";

/** Failure code from the server -> the sentence the user reads.
 *
 *  The server sends a stable slug; the wording lives here so it can be
 *  translated, and so an unknown code (older backend, new failure) falls
 *  back to a generic sentence instead of rendering a raw provider dump. */
const CHAT_ERROR_KEYS: Record<string, string> = {
  no_api_key: "projects.chats.detail.noApiKey",
  invalid_key: "projects.chats.detail.errorInvalidKey",
  rate_limited: "projects.chats.detail.errorRateLimit",
  quota_exhausted: "projects.chats.detail.errorQuotaExhausted",
  model_not_found: "projects.chats.detail.errorModelNotFound",
  context_too_long: "projects.chats.detail.errorContextTooLong",
  provider_unavailable: "projects.chats.detail.errorProviderUnavailable",
  budget_exceeded: "projects.chats.detail.errorBudgetExceeded",
};

export default function ChatPage({
  params,
}: {
  params: Promise<{ id: string; chatId: string }>;
}) {
  const { id: projectId, chatId } = use(params);
  const token = useToken();
  const qc = useQueryClient();
  const t = useT();
  const { confirm, dialog } = useConfirm();

  const chat = useQuery({
    queryKey: ["chat", chatId],
    queryFn: () => chatsApi.get(token!, chatId),
    enabled: !!token,
  });

  // Local streaming state (поверх persisted messages)
  const [draft, setDraft] = useState("");
  // Stage 22 — show/hide source code in answers (toggle). Persisted per-browser.
  const [includeCode, setIncludeCode] = useState(true);
  useEffect(() => {
    const saved = localStorage.getItem("qa_include_code");
    if (saved != null) setIncludeCode(saved === "1");
  }, []);
  const toggleCode = () => {
    setIncludeCode((v) => {
      localStorage.setItem("qa_include_code", v ? "0" : "1");
      return !v;
    });
  };
  const [streamingMsg, setStreamingMsg] = useState<{
    content: string;
    meta: MessageMeta | null;
  } | null>(null);
  const [optimisticUser, setOptimisticUser] = useState<string | null>(null);
  // Kept out of the thread: an error is about the attempt, not the
  // conversation, and it must stay visible without scrolling on a phone.
  const [streamError, setStreamError] =
    useState<{ message: string; hint: string } | null>(null);
  /** Vault (vector store) missing for this workspace. `undefined` = no live
   *  answer yet in this session, so fall back to the persisted history. The
   *  banner is rendered once per chat, not once per message. */
  const [liveVaultUnavailable, setLiveVaultUnavailable] =
    useState<string | null | undefined>(undefined);

  const sse = useSSE();

  // Same route segment across chats — drop the live flag so the banner never
  // leaks from the previously opened chat.
  useEffect(() => setLiveVaultUnavailable(undefined), [chatId]);

  const persistedVaultUnavailable = useMemo(() => {
    const msgs = chat.data?.messages ?? [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role === "assistant" && m.meta) return m.meta.vault_unavailable || null;
    }
    return null;
  }, [chat.data?.messages]);
  const vaultUnavailable =
    liveVaultUnavailable !== undefined
      ? liveVaultUnavailable
      : persistedVaultUnavailable;

  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll до низу коли нові tokens/messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [chat.data?.messages_count, streamingMsg?.content]);

  const handleSend = async () => {
    const content = draft.trim();
    if (!content || sse.status === "streaming") return;
    setDraft("");
    setStreamError(null);
    setOptimisticUser(content);
    setStreamingMsg({ content: "", meta: null });

    await sse.send({
      url: askUrl(chatId),
      token,
      body: { content, stream: true, include_code: includeCode },
      onEvent: (ev, data) => {
        const d = data as Record<string, unknown>;
        if (ev === "meta") {
          // Not an error: retrieval degraded to grep + graph + code. Surfaced
          // as one banner above the composer instead of a toast.
          setLiveVaultUnavailable((d.vault_unavailable as string) || null);
          setStreamingMsg((prev) => prev && {
            ...prev,
            meta: {
              ...prev.meta,
              vault_hits: (d.vault_hits as MessageMeta["vault_hits"]) ?? [],
              files_read_count: (d.files_read_count as number) ?? 0,
              blocked_repos: (d.blocked_repos as string[]) ?? [],
              hidden_files_count: (d.hidden_files_count as number) ?? 0,
              code_included: (d.code_included as boolean) ?? true,
            },
          });
        } else if (ev === "delta") {
          const text = (d.text as string) ?? "";
          setStreamingMsg((prev) => prev && {
            ...prev,
            content: prev.content + text,
          });
        } else if (ev === "done") {
          setStreamingMsg((prev) => prev && {
            ...prev,
            meta: {
              ...prev.meta,
              tokens_in: (d.tokens_in as number) ?? 0,
              tokens_out: (d.tokens_out as number) ?? 0,
              elapsed_s: (d.elapsed_s as number) ?? null,
              citations_total: (d.citations_total as number) ?? 0,
              citations_invalid: (d.citations_invalid as number) ?? 0,
            },
          });
        } else if (ev === "error") {
          // One short sentence in the toast, the technical hint behind a
          // disclosure below. The server used to send the provider's whole
          // response body here and it was interpolated straight into the
          // toast, which has no height cap — on a phone that is most of the
          // screen, in text nobody can act on.
          const key = CHAT_ERROR_KEYS[String(d.code ?? "")]
            ?? "projects.chats.detail.errorGeneric";
          const message = t(key);
          const hint = d.detail ? String(d.detail).slice(0, 300) : "";
          setStreamError({ message, hint });
          toast.error(message);
        }
      },
    });
    // Після завершення — invalidate щоб persisted state підтягнувся
    setTimeout(() => {
      qc.invalidateQueries({ queryKey: ["chat", chatId] });
      setStreamingMsg(null);
      setOptimisticUser(null);
    }, 200);
  };

  const handleStop = () => sse.cancel();

  if (chat.isLoading) {
    return <div className="p-8 text-center text-muted-foreground">{t("projects.chats.detail.loading")}</div>;
  }
  if (chat.error) {
    return <div className="p-8 text-destructive">{(chat.error as Error).message}</div>;
  }
  if (!chat.data) return null;

  return (
    <div className="flex flex-col h-screen max-h-screen">
      {/* Header */}
      <div className="border-b px-6 py-3 flex items-center justify-between">
        <div>
          <Link
            href={`/projects/${projectId}`}
            className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
          >
            <ArrowLeftIcon className="h-3 w-3" />
            {t("projects.chats.detail.backToProject")}
          </Link>
          <h1 className="font-medium text-sm mt-1">
            {chat.data.name || t("projects.chats.detail.chatFallback", { id: chatId.slice(0, 8) })}
          </h1>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={async () => {
            const ok = await confirm({
              title: t("projects.chats.detail.clearConfirm"),
              danger: true,
            });
            if (ok) {
              await chatsApi.clear(token!, chatId);
              qc.invalidateQueries({ queryKey: ["chat", chatId] });
            }
          }}
        >
          <TrashIcon className="h-4 w-4 mr-1" />
          {t("projects.chats.detail.clearButton")}
        </Button>
      </div>

      {/* Messages scroll area */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        <MessageList
          messages={chat.data.messages}
          optimisticUser={optimisticUser}
          streamingMsg={streamingMsg}
        />
        {chat.data.messages.length === 0 && !streamingMsg && (
          <div className="text-center text-muted-foreground py-12">
            <p className="text-sm">{t("projects.chats.detail.emptyTitle")}</p>
            <p className="text-xs mt-2">
              {t("projects.chats.detail.emptyHint")}
            </p>
          </div>
        )}
      </div>

      {/* Input */}
      <div className="border-t px-6 py-3">
        {vaultUnavailable && (
          <Callout tone="warning" className="mb-2">
            {t("projects.chats.detail.vaultMissing")}{" "}
            <Link href="/repositories" className="font-medium underline">
              {t("projects.chats.detail.vaultMissingCta")}
            </Link>
          </Callout>
        )}
        <div className="mb-2 flex items-center gap-2">
          <button
            type="button"
            onClick={toggleCode}
            title={t("projects.chats.detail.toggleCodeTitle")}
            className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors ${
              includeCode
                ? "border-primary/30 bg-primary/10 text-primary"
                : "border-border text-muted-foreground hover:bg-accent"
            }`}
          >
            {includeCode ? (
              <CodeIcon className="h-3.5 w-3.5" />
            ) : (
              <EyeOffIcon className="h-3.5 w-3.5" />
            )}
            {includeCode
              ? t("projects.chats.detail.codeOn")
              : t("projects.chats.detail.codeOff")}
          </button>
          <span className="text-[11px] text-muted-foreground">
            {includeCode
              ? t("projects.chats.detail.codeOnHint")
              : t("projects.chats.detail.codeOffHint")}
          </span>
        </div>
        <div className="flex gap-2 items-end">
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder={t("projects.chats.detail.inputPlaceholder")}
            className="min-h-[60px]"
            disabled={sse.status === "streaming"}
          />
          {sse.status === "streaming" ? (
            <Button onClick={handleStop} variant="destructive" size="lg">
              <StopCircleIcon className="h-4 w-4" />
            </Button>
          ) : (
            <Button onClick={handleSend} disabled={!draft.trim()} size="lg">
              <SendIcon className="h-4 w-4" />
            </Button>
          )}
        </div>
        {(streamError || sse.error) && (
          <div className="mt-2 rounded-md border border-[var(--color-destructive)]/40 bg-[var(--color-destructive)]/10 px-2 py-1.5 text-xs">
            <div className="flex items-start gap-2">
              {/* line-clamp-2 plus the capped <pre> below are what make
                  this unrepeatable: whatever the provider sends, the box
                  is at most a few lines tall. */}
              <p className="line-clamp-2 min-w-0 flex-1 wrap-anywhere text-[var(--color-destructive)]">
                {streamError?.message ?? sse.error}
              </p>
              <button
                type="button"
                aria-label={t("projects.chats.detail.errorDismiss")}
                className="shrink-0 opacity-70 hover:opacity-100"
                onClick={() => setStreamError(null)}
              >
                <XIcon className="h-3.5 w-3.5" />
              </button>
            </div>
            {streamError?.hint && (
              <details className="mt-1">
                <summary className="cursor-pointer opacity-70 hover:opacity-100">
                  {t("projects.chats.detail.errorDetailsToggle")}
                </summary>
                <pre className="mt-1 max-h-24 overflow-y-auto overscroll-contain whitespace-pre-wrap wrap-anywhere rounded bg-[var(--color-secondary)] p-2 text-[11px]">
                  {streamError.hint}
                </pre>
              </details>
            )}
          </div>
        )}
      </div>
      {dialog}
    </div>
  );
}

function MessageList({
  messages, optimisticUser, streamingMsg,
}: {
  messages: MessageOut[];
  optimisticUser: string | null;
  streamingMsg: { content: string; meta: MessageMeta | null } | null;
}) {
  return (
    <>
      {messages.map((m) => (
        <MessageBubble
          key={m.id}
          role={m.role}
          content={m.content}
          meta={m.meta}
        />
      ))}
      {optimisticUser && (
        <MessageBubble role="user" content={optimisticUser} meta={null} optimistic />
      )}
      {streamingMsg && (
        <MessageBubble
          role="assistant"
          content={streamingMsg.content}
          meta={streamingMsg.meta}
          streaming
        />
      )}
    </>
  );
}

function MessageBubble({
  role, content, meta, optimistic, streaming,
}: {
  role: "user" | "assistant";
  content: string;
  meta: MessageMeta | null;
  optimistic?: boolean;
  streaming?: boolean;
}) {
  const t = useT();
  const isUser = role === "user";
  return (
    <div className={isUser ? "flex justify-end" : "flex"}>
      <Card
        className={`max-w-[85%] ${
          isUser ? "bg-primary/10 border-primary/20" : "bg-card"
        } ${optimistic ? "opacity-70" : ""}`}
      >
        <CardContent className="py-3 px-4 space-y-2">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="font-medium">
              {isUser ? t("projects.chats.detail.roleUser") : t("projects.chats.detail.roleAssistant")}
            </span>
            {streaming && <LoaderIcon className="h-3 w-3 animate-spin" />}
            {meta?.elapsed_s != null && (
              <span>· {meta.elapsed_s}s</span>
            )}
            {meta?.tokens_out != null && meta.tokens_out > 0 && (
              <span>· {meta.tokens_in}→{meta.tokens_out} {t("projects.chats.detail.tokensLabel")}</span>
            )}
          </div>

          {!isUser && meta?.blocked_repos && meta.blocked_repos.length > 0 && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
              <ShieldAlertIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                {t("projects.chats.detail.blockedReposPrefix")}
                <b>{meta.blocked_repos.join(", ")}</b>
                {t("projects.chats.detail.blockedReposSuffix")}
              </span>
            </div>
          )}
          {!isUser && meta?.hidden_files_count != null && meta.hidden_files_count > 0 && (
            <div className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-1.5 text-[11px] text-muted-foreground">
              <EyeOffIcon className="h-3 w-3 shrink-0" />
              {t("projects.chats.detail.hiddenFiles", { count: meta.hidden_files_count })}
            </div>
          )}
          {!isUser && meta?.citations_invalid != null && meta.citations_invalid > 0 && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-700 dark:text-amber-400">
              <ShieldAlertIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                {t("projects.chats.detail.citationsUnverified", {
                  invalid: meta.citations_invalid,
                  total: meta.citations_total ?? 0,
                })}
                {meta.citations_bad?.length ? (
                  <span className="mt-1 block font-mono opacity-80">
                    {meta.citations_bad.slice(0, 3).map((c, i) => (
                      <span key={i} className="block">
                        {c.path}:{c.line} — {c.status}
                      </span>
                    ))}
                  </span>
                ) : null}
              </span>
            </div>
          )}
          {!isUser && meta?.citations_total != null && meta.citations_total > 0 && (meta.citations_invalid ?? 0) === 0 && (
            <div className="flex items-center gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-[11px] text-emerald-700 dark:text-emerald-400">
              <CheckIcon className="h-3 w-3 shrink-0" />
              {t("projects.chats.detail.citationsVerified", { total: meta.citations_total })}
            </div>
          )}
          {!isUser && meta?.code_included === false && (
            <div className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-3 py-1.5 text-[11px] text-muted-foreground">
              <EyeOffIcon className="h-3 w-3 shrink-0" />
              {t("projects.chats.detail.codeDisabledNotice")}
            </div>
          )}

          {isUser ? (
            <div className="whitespace-pre-wrap wrap-anywhere text-sm font-mono leading-relaxed">
              {content || (streaming && "…")}
            </div>
          ) : content ? (
            <Markdown text={content} />
          ) : (
            <div className="text-sm leading-relaxed">{streaming && "…"}</div>
          )}

          {meta?.vault_hits && meta.vault_hits.length > 0 && (
            <div className="pt-2 border-t border-border/50 space-y-1">
              <div className="text-[10px] text-muted-foreground uppercase tracking-wide">
                {t("projects.chats.detail.sources", { count: meta.vault_hits.length })}
                {meta.files_read_count != null && (
                  <span> · {t("projects.chats.detail.filesRead", { count: meta.files_read_count })}</span>
                )}
              </div>
              <div className="flex flex-wrap gap-1">
                {meta.vault_hits.slice(0, 8).map((h, i) => (
                  <Badge key={i} variant="outline" className="text-[10px]">
                    <FileTextIcon className="h-2.5 w-2.5 mr-1" />
                    {h.repo ? `${h.repo}/` : ""}{h.note_path}
                    <span className="ml-1 opacity-60">
                      {h.score.toFixed(2)}
                    </span>
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
