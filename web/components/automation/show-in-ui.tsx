"use client";

/**
 * "Showing you: …" — the card under an answer when the agent opened a page for
 * the person instead of describing it.
 *
 * A fresh answer starts by itself (once; `shouldAutoRun`), because the whole
 * point of asking "where do I add custom prompts for repo X" is to be taken
 * there. The button runs it again: the tour is dismissable (Esc), and a
 * person who closed it by accident should not have to ask the question again.
 * A person whose role cannot change the page gets the link and the roles that
 * can, never a spotlight on a disabled field (`planUiAction`).
 *
 * driver.js is loaded on the first run, with the rest of the tour code
 * (lib/guide-run.ts).
 */

import { useCallback, useEffect, useRef } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { CrosshairIcon, ExternalLinkIcon, RotateCcwIcon } from "lucide-react";

import { workspacesApi } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useToken } from "@/lib/use-token";
import {
  planUiAction, shouldAutoRun, type UiAction,
} from "@/lib/ui-action";

/** Run ids already acted on in this tab; the set survives the card remounting
 *  (the thread re-renders on every poll) but not a reload of the tab. */
const SEEN_KEY = "celmis:ui-action-seen";

function readSeen(): Set<string> {
  try {
    return new Set(JSON.parse(sessionStorage.getItem(SEEN_KEY) ?? "[]") as string[]);
  } catch {
    return new Set();
  }
}

function remember(runId: string): void {
  try {
    const seen = [...readSeen(), runId].slice(-50);
    sessionStorage.setItem(SEEN_KEY, JSON.stringify(seen));
  } catch {
    /* private mode: the worst case is one repeated tour after a reload */
  }
}

export function ShowInUi({
  ui, runId, createdAt,
}: {
  ui: UiAction;
  runId: string;
  createdAt: string;
}) {
  const t = useT();
  const router = useRouter();
  const token = useToken();
  const alive = useRef(true);
  const me = useQuery({
    queryKey: ["workspaces-me"],
    queryFn: () => workspacesApi.me(token!),
    enabled: !!token,
  });
  const active = me.data?.active_id ?? "";
  // Derived, not state: it only depends on the answer and the workspace.
  const stale = planUiAction(ui, active).kind === "stale";

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const run = useCallback(async () => {
    const plan = planUiAction(ui, active);
    if (plan.kind === "stale") return;
    if (plan.kind === "open") { router.push(plan.route); return; }
    const { runGuide } = await import("@/lib/guide-run");
    if (!alive.current) return;
    await runGuide(plan.steps, { t, navigate: (route) => router.push(route) });
  }, [ui, active, router, t]);

  // Once per answer, only while it is fresh, and only after the workspace is
  // known: acting before that would compare against an empty id.
  const started = useRef(false);
  useEffect(() => {
    if (started.current || !runId || !active || ui.mode !== "tour") return;
    if (!shouldAutoRun(runId, Date.parse(createdAt), Date.now(), readSeen())) return;
    started.current = true;
    remember(runId);
    void run();
  }, [runId, createdAt, active, ui.mode, run]);

  return (
    <div
      data-testid="show-in-ui"
      className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--color-brand)]/30 bg-[var(--color-brand)]/5 px-3 py-2 text-xs"
    >
      <CrosshairIcon className="h-3.5 w-3.5 shrink-0 text-[var(--color-brand)]" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="font-medium wrap-anywhere">{t("automation.ui.showing", { title: ui.title })}</div>
        {ui.repo && (
          <div className="text-[var(--color-muted-foreground)] wrap-anywhere">
            {t("automation.ui.repo", { repo: ui.repo })}
          </div>
        )}
        {ui.mode === "link" && (
          <div className="mt-0.5 text-[var(--color-muted-foreground)]">
            {t("automation.ui.needs", { roles: ui.needs.join(", ") })}
          </div>
        )}
      </div>
      {ui.mode === "link" || ui.steps.length === 0 ? (
        <Link
          href={ui.route}
          className="inline-flex items-center gap-1 rounded-full border border-[var(--color-border)] px-2.5 py-1 font-medium hover:bg-[var(--color-accent)]"
        >
          <ExternalLinkIcon className="h-3 w-3" aria-hidden />
          {t("automation.ui.open")}
        </Link>
      ) : (
        <button
          type="button"
          onClick={() => { void run(); }}
          className="inline-flex items-center gap-1 rounded-full border border-[var(--color-brand)]/40 px-2.5 py-1 font-medium text-[var(--color-brand)] hover:bg-[var(--color-brand)]/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]"
        >
          <RotateCcwIcon className="h-3 w-3" aria-hidden />
          {t("automation.ui.again")}
        </button>
      )}
      {stale && <div className="w-full text-[var(--color-muted-foreground)]">{t("automation.ui.stale")}</div>}
    </div>
  );
}
