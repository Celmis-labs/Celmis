/**
 * The client half of the agent's `show_in_ui` action.
 *
 * The server (src/automation/ui_catalogue.py) answers "I need custom prompts
 * for repo X" with a validated action: the route that opens the right page
 * with the repository selected, the controls to spotlight and, when the
 * asker's role cannot change that page, a "link" instead of a "tour". This
 * module decides what the browser does with it. It is pure — no DOM, no
 * router — so the decisions (which are the ones that go wrong quietly) are
 * unit-tested; `components/automation/show-in-ui.tsx` is the thin shell that
 * performs them with `runGuide` from lib/guide-run.ts.
 *
 * The action is data from our own server, stored in a run row, and is treated
 * as untrusted anyway: an unknown shape degrades to "no card".
 */

import { isInAppRoute, sanitizeSteps, type GuideStep } from "@/lib/guide-tour";

export type UiAction = {
  /** "tour": spotlight the fields. "link": the asker's role cannot change
   *  this page, so only offer it (with the roles that can). */
  mode: "tour" | "link";
  /** The page title shown on the card: "Review settings > Custom prompts". */
  title: string;
  route: string;
  /** Repository slug the page is opened on; empty for workspace-wide pages. */
  repo: string;
  workspaceId: string;
  needs: string[];
  steps: GuideStep[];
};

export function sanitizeUiAction(raw: unknown, steps: unknown): UiAction | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.route !== "string" || !isInAppRoute(r.route)) return null;
  const clean = sanitizeSteps(steps ?? r.steps);
  return {
    mode: r.mode === "link" ? "link" : "tour",
    title: typeof r.title === "string" ? r.title.slice(0, 120) : "",
    route: r.route,
    repo: typeof r.repo === "string" ? r.repo.slice(0, 200) : "",
    workspaceId: typeof r.workspace_id === "string" ? r.workspace_id : "",
    needs: Array.isArray(r.needs) ? r.needs.filter((x): x is string => typeof x === "string") : [],
    steps: clean,
  };
}

/** What pressing "show" (or arriving fresh) should do. */
export type UiPlan =
  | { kind: "tour"; steps: GuideStep[] }
  | { kind: "open"; route: string }
  | { kind: "stale" };

/** Decide what to do. The action was resolved against the workspace the
 *  question was asked in (its repositories, its roles). If another workspace
 *  is active now the route would open a repository of the wrong one, so the
 *  answer is "stale": nothing runs, and the card says so. A "link" never starts a tour: its fields are
 *  read-only for this person, and a spotlight on a disabled input reads as a
 *  bug. A tour with no valid steps is just the page. */
export function planUiAction(ui: UiAction, activeWorkspace: string): UiPlan {
  if (ui.workspaceId && activeWorkspace && ui.workspaceId !== activeWorkspace) {
    return { kind: "stale" };
  }
  if (ui.mode === "link" || ui.steps.length === 0) return { kind: "open", route: ui.route };
  return { kind: "tour", steps: ui.steps };
}

/** An answer is acted on by itself only once, and only while it is fresh: the
 *  same reply reloaded from history, or opened on another day, must not start
 *  a tour nobody asked for. `seen` is the set of run ids already acted on. */
export const AUTO_RUN_WINDOW_MS = 90_000;

export function shouldAutoRun(
  runId: string, createdAtMs: number, nowMs: number, seen: ReadonlySet<string>,
): boolean {
  if (!runId || seen.has(runId)) return false;
  return Number.isFinite(createdAtMs) && nowMs - createdAtMs <= AUTO_RUN_WINDOW_MS;
}
