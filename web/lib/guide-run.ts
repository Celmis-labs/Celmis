"use client";

/**
 * The driver.js half of "Show me" (see lib/guide-tour.ts for the rest).
 *
 * One tour at a time: starting another, or leaving the page's control behind,
 * stops the first. Esc and the close button end it (driver.js handles both and
 * returns focus); motion is switched off for people who asked the system for
 * less of it.
 */

import { driver, type DriveStep, type Driver } from "driver.js";
import "driver.js/dist/driver.css";

import { AGENT_PANEL_EVENT } from "@/lib/agent-session";
import {
  groupByRoute, resolveGroup, selectorOf, type GuideEnv, type GuideStep, type Resolved,
} from "@/lib/guide-tour";

type Translate = (key: string, vars?: Record<string, string | number>) => string;

let active: { driver: Driver | null; cancelled: boolean } | null = null;

const PULSE = "celmis-tour-pulse";

function reducedMotion(): boolean {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

function panel(open: boolean) {
  window.dispatchEvent(new CustomEvent(AGENT_PANEL_EVENT, { detail: { open } }));
}

/** Stop whatever tour is running (a new answer, a new tour, unmount). */
export function stopGuide(): void {
  if (!active) return;
  active.cancelled = true;
  active.driver?.destroy();
  active = null;
}

function driveGroup(
  items: Resolved[], t: Translate, last: boolean, still: boolean,
): Promise<"done" | "closed"> {
  return new Promise((resolve) => {
    let finished = false;
    const total = items.length;
    const steps: DriveStep[] = items.map(({ step, el }) => ({
      element: el ?? undefined,
      popover: {
        description: el ? step.text : `${step.text}\n\n${t("guide.missing")}`,
        // Inside an unfolded card the fields continue below the target, so
        // the sentence goes above it; driver.js flips it if there is no room.
        side: step.opener ? "top" : "bottom",
        align: "start",
      },
    }));
    const d = driver({
      steps,
      showProgress: total > 1,
      progressText: t("tour.progress"),
      nextBtnText: t("tour.next"),
      prevBtnText: t("tour.prev"),
      // The last step of an earlier page continues on the next one.
      doneBtnText: last ? t("tour.done") : t("tour.next"),
      popoverClass: "celmis-tour",
      animate: !still,
      smoothScroll: !still,
      stagePadding: 6,
      stageRadius: 8,
      allowClose: true,
      onHighlighted: (el) => { el?.classList.add(PULSE); },
      onDeselected: (el) => { el?.classList.remove(PULSE); },
      // `onNextClick` on the final step of a non-final group would end the
      // whole tour by default; reaching it is "continue", closing is not.
      onNextClick: () => {
        if (d.isLastStep()) {
          finished = true;
          d.destroy();
        } else {
          d.moveNext();
        }
      },
      onDestroyed: () => resolve(finished ? "done" : "closed"),
    });
    if (active) active.driver = d;
    d.drive();
  });
}

/** Walk the person through `steps`. Resolves when the tour ends. */
export async function runGuide(
  steps: GuideStep[], opts: { t: Translate; navigate: (route: string) => void },
): Promise<void> {
  stopGuide();
  const me: { driver: Driver | null; cancelled: boolean } = { driver: null, cancelled: false };
  active = me;
  const env: GuideEnv = {
    here: () => window.location.pathname + window.location.search,
    go: opts.navigate,
    find: (id) => document.querySelector<HTMLElement>(selectorOf(id)),
    click: (el) => el.click(),
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    cancelled: () => me.cancelled,
  };
  const still = reducedMotion();
  const groups = groupByRoute(steps);
  panel(false);
  try {
    for (const [gi, group] of groups.entries()) {
      const resolved = await resolveGroup(group, env);
      if (me.cancelled) return;
      const end = await driveGroup(resolved, opts.t, gi === groups.length - 1, still);
      if (end === "closed" || me.cancelled) return;
    }
  } finally {
    if (active === me) active = null;
    if (!me.cancelled) panel(true);
  }
}
