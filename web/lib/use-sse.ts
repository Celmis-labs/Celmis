"use client";

/**
 * useSSE — minimal SSE consumer для POST endpoints з custom headers (Bearer auth).
 *
 * Чому не EventSource: EventSource = GET-only, не вміє Authorization header
 * без proxy. Vanilla fetch + ReadableStream дає повний контроль.
 *
 * Usage:
 *
 *   const { send, status, events } = useSSE();
 *   await send({
 *     url: askUrl(chatId),
 *     token,
 *     body: { content, stream: true },
 *     onEvent: (ev, data) => {
 *       if (ev === "delta") setText(t => t + data.text);
 *     },
 *   });
 */

import { useCallback, useRef, useState } from "react";

export type SSEStatus = "idle" | "streaming" | "done" | "error" | "cancelled";

export interface SSESendOptions {
  url: string;
  token: string | null;
  body?: unknown;
  /** default POST; GET for resumable tail streams (no body) */
  method?: "POST" | "GET";
  /** eventId = SSE `id:` field — the cursor for resuming a dropped stream */
  onEvent: (eventName: string, data: unknown, eventId?: string) => void;
  signal?: AbortSignal;
}

export interface UseSSEResult {
  status: SSEStatus;
  error: string | null;
  send: (opts: SSESendOptions) => Promise<void>;
  cancel: () => void;
}

export function useSSE(): UseSSEResult {
  const [status, setStatus] = useState<SSEStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    setStatus("cancelled");
  }, []);

  const send = useCallback(async (opts: SSESendOptions) => {
    setStatus("streaming");
    setError(null);

    const controller = new AbortController();
    abortRef.current = controller;
    // Поєднати externally provided signal
    if (opts.signal) {
      opts.signal.addEventListener("abort", () => controller.abort(), {
        once: true,
      });
    }

    const method = opts.method ?? "POST";
    const headers: Record<string, string> = {
      Accept: "text/event-stream",
    };
    if (method === "POST") headers["Content-Type"] = "application/json";
    if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
    // Активний воркспейс — як у api.ts: без цього SSE-запити (ask, agent
    // stream) резолвляться у воркспейс за замовчуванням, а не в обраний.
    const wsMatch = typeof document !== "undefined"
      ? document.cookie.match(/(?:^|;\s*)x-workspace=([^;]+)/)
      : null;
    if (wsMatch) headers["X-Workspace"] = decodeURIComponent(wsMatch[1]);

    let resp: Response;
    try {
      resp = await fetch(opts.url, {
        method,
        headers,
        body: method === "POST" ? JSON.stringify(opts.body) : undefined,
        signal: controller.signal,
      });
    } catch (e) {
      if (controller.signal.aborted) {
        setStatus("cancelled");
        return;
      }
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      setStatus("error");
      return;
    }

    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      setError(`HTTP ${resp.status}: ${text.slice(0, 200)}`);
      setStatus("error");
      return;
    }
    if (!resp.body) {
      setError("response has no body");
      setStatus("error");
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let currentEvent = "message";

    try {
      // SSE парсер: рядки розділені \n\n, поля event:/data:/id:/retry:
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // Нормалізуємо CRLF → LF: sse-starlette (agent-sessions, QA) шле
        // \r\n-розділені фрейми, а парсер нижче шукає "\n\n". Самотній \r
        // в кінці buffer лишається до наступного chunk'а і нормалізується
        // наступним проходом.
        buffer = buffer.replace(/\r\n/g, "\n");

        // Обробляємо повні events (розділені порожнім рядком)
        let nlnl: number;
        // eslint-disable-next-line no-cond-assign
        while ((nlnl = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, nlnl);
          buffer = buffer.slice(nlnl + 2);
          if (!raw.trim()) continue;

          let evName = "message";
          let evId: string | undefined;
          let dataLines: string[] = [];
          for (const line of raw.split("\n")) {
            if (line.startsWith(":")) continue; // comment / ping
            if (line.startsWith("event:")) {
              evName = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).trim());
            } else if (line.startsWith("id:")) {
              evId = line.slice(3).trim();
            } else if (line.startsWith("retry:")) {
              continue; // reconnect delay is the caller's business
            }
          }
          if (dataLines.length === 0) continue;
          const dataStr = dataLines.join("\n");
          let parsed: unknown = dataStr;
          try {
            parsed = JSON.parse(dataStr);
          } catch {
            // лишаємо як string
          }
          currentEvent = evName;
          try {
            opts.onEvent(evName, parsed, evId);
          } catch (handlerErr) {
            console.error("SSE onEvent handler error:", handlerErr);
          }

          if (evName === "done" || evName === "error") {
            setStatus(evName === "done" ? "done" : "error");
            // Не повертаємось — даємо stream дочитатися
          }
        }
      }
      if (status !== "error") setStatus("done");
    } catch (e) {
      if (controller.signal.aborted) {
        setStatus("cancelled");
        return;
      }
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      setStatus("error");
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // ignore
      }
    }
    // suppress unused
    void currentEvent;
  }, [status]);

  return { status, error, send, cancel };
}
