/**
 * "Show me": the agent's answer walks the person to the control it talks about.
 *
 * The server (src/automation/tour.py) validates every step against a table of
 * known targets and attaches the route to open and, for controls inside a
 * folded container, the id of the control that unfolds it. This module is the
 * browser half: open the page, wait for the control, unfold what hides it, and
 * hand the elements to driver.js, which dims the page, cuts a hole around the
 * control and shows the sentence. Targets are `data-tour="…"` attributes, so
 * nothing here depends on translated text or DOM structure.
 *
 * The pure parts (`sanitizeSteps`, `groupByRoute`, `resolveGroup`) take their
 * DOM as an argument and are unit-tested; `runGuide` is the thin driver.js shell.
 */

export type GuideStep = {
  /** `data-tour` value of the control. */
  target: string;
  /** One sentence, already in the language of the answer. */
  text: string;
  /** In-app path (with query) the control lives on. */
  route: string;
  /** `data-tour` value of the control that unfolds the container, if any. */
  opener?: string;
};

export const MAX_GUIDE_STEPS = 8;

const ID = /^[a-z][a-z0-9-]*(:[a-z_]+)?$/;

/** A path inside this app: one leading slash, no scheme, no backslash. */
export function isInAppRoute(route: string): boolean {
  return /^\/(?!\/)[^\\\s]*$/.test(route);
}

/** The steps of a stored answer that are safe to act on. The row came from our
 *  own server, but it is data: a malformed or hand-edited step must degrade to
 *  "no button", never to an exception or a selector built from free text. */
export function sanitizeSteps(raw: unknown): GuideStep[] {
  if (!Array.isArray(raw)) return [];
  const out: GuideStep[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const { target, text, route, opener } = item as Record<string, unknown>;
    if (typeof target !== "string" || !ID.test(target)) continue;
    if (typeof text !== "string" || !text.trim()) continue;
    if (typeof route !== "string" || !isInAppRoute(route)) continue;
    if (opener !== undefined && (typeof opener !== "string" || !ID.test(opener))) continue;
    out.push({
      target, route, text: text.trim().slice(0, 400),
      ...(opener ? { opener } : {}),
    });
    if (out.length === MAX_GUIDE_STEPS) break;
  }
  return out;
}

export const selectorOf = (id: string): string => `[data-tour="${id}"]`;

/** Consecutive steps on the same page form one group: one navigation, one
 *  spotlight sequence. A tour across two pages is two groups. */
export function groupByRoute(steps: GuideStep[]): GuideStep[][] {
  const groups: GuideStep[][] = [];
  for (const s of steps) {
    const last = groups[groups.length - 1];
    if (last && last[0].route === s.route) last.push(s);
    else groups.push([s]);
  }
  return groups;
}

/** What the resolver needs from the page. Injected so it runs without one. */
export type GuideEnv = {
  /** `pathname + search` right now. */
  here(): string;
  go(route: string): void;
  find(id: string): HTMLElement | null;
  click(el: HTMLElement): void;
  sleep(ms: number): Promise<void>;
  cancelled(): boolean;
};

export type Resolved = { step: GuideStep; el: HTMLElement | null };

const POLL_MS = 150;
const PAGE_WAIT_MS = 6000;
const OPEN_WAIT_MS = 1500;
/** Time for a folded container's own animation before measuring. */
const SETTLE_MS = 320;

async function waitFor(
  env: GuideEnv, id: string, budget: number,
): Promise<HTMLElement | null> {
  for (let waited = 0; ; waited += POLL_MS) {
    const el = env.find(id);
    if (el || waited >= budget || env.cancelled()) return el;
    await env.sleep(POLL_MS);
  }
}

/** Open the group's page and find every control on it. A control hidden in a
 *  folded container is revealed by pressing its opener once; one that still is
 *  not there (a role that cannot see it, a setting that hides it) comes back
 *  as `el: null`, and the caller shows its sentence without a spotlight. An
 *  opener that is already open (and only serves later steps) is dropped. */
export async function resolveGroup(
  group: GuideStep[], env: GuideEnv,
): Promise<Resolved[]> {
  if (env.here() !== group[0].route) env.go(group[0].route);
  const out: Resolved[] = [];
  for (const [i, step] of group.entries()) {
    let el = await waitFor(env, step.target, i === 0 ? PAGE_WAIT_MS : OPEN_WAIT_MS);
    if (!el && step.opener && !env.cancelled()) {
      const opener = await waitFor(env, step.opener, i === 0 ? PAGE_WAIT_MS : OPEN_WAIT_MS);
      if (opener && opener.getAttribute("aria-expanded") !== "true") {
        env.click(opener);
        el = await waitFor(env, step.target, OPEN_WAIT_MS);
      }
    }
    // A toggle whose container is already open, and which a later step of this
    // group only uses as an opener: its "click to open" sentence (written by the
    // model, so not trusted to know the state) would be stale and its popover
    // would sit on the opened fields. Go straight to the inner control.
    if (
      el && el.getAttribute("aria-expanded") === "true"
      && group.some((s, j) => j > i && s.opener === step.target)
    ) continue;
    if (el) await env.sleep(SETTLE_MS);
    out.push({ step, el });
    if (env.cancelled()) break;
  }
  return out;
}
