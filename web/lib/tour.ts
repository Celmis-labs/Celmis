"use client";

/**
 * driver.js product tour.
 *
 * Two entry points:
 *   - startMainTour(t)  — the 7-step orientation tour over the sidebar nav and
 *     the workspace switcher. Auto-started once on /dashboard (see AppShell)
 *     and restartable from the dashboard tips banner.
 *   - startSpotlight()  — a single-element highlight used by the onboarding
 *     "show me where" buttons: the source page stores the target in
 *     sessionStorage, navigates, and the destination page calls this.
 *
 * Targets are `data-tour="…"` attributes so the tour never depends on
 * translated text or DOM structure. The driver.css import is global CSS —
 * this module must only ever be imported from client components (AppShell,
 * dashboard, onboarding, repositories, claude), which it is.
 */

import { useEffect } from "react";
import { driver, type DriveStep } from "driver.js";
import "driver.js/dist/driver.css";

type Translate = (key: string, vars?: Record<string, string | number>) => string;

export const TOUR_DONE_KEY = "celmis:tour-done";
export const SPOTLIGHT_KEY = "celmis:spotlight";

/** Payload passed between pages for the one-off "show me where" spotlight. */
export type SpotlightPayload = { el: string; title: string; desc: string };

function baseConfig(t: Translate) {
  return {
    showProgress: true,
    // Custom class hooks the theme overrides in globals.css — needed because
    // driver.css loads after globals.css, so bare .driver-popover rules there
    // would lose the cascade at equal specificity.
    popoverClass: "celmis-tour",
    progressText: t("tour.progress"),
    nextBtnText: t("tour.next"),
    prevBtnText: t("tour.prev"),
    doneBtnText: t("tour.done"),
  };
}

export function startMainTour(t: Translate) {
  const steps: DriveStep[] = [
    {
      element: '[data-tour="nav"]',
      popover: {
        title: t("tour.step.nav.title"),
        description: t("tour.step.nav.desc"),
        side: "right",
        align: "start",
      },
    },
    {
      // The single most important concept: everything is scoped to the
      // active workspace picked in the topbar.
      element: '[data-tour="workspace"]',
      popover: {
        title: t("tour.step.workspace.title"),
        description: t("tour.step.workspace.desc"),
        side: "bottom",
        align: "start",
      },
    },
    {
      element: '[data-tour="nav-repos"]',
      popover: {
        title: t("tour.step.repos.title"),
        description: t("tour.step.repos.desc"),
        side: "right",
        align: "start",
      },
    },
    {
      element: '[data-tour="nav-qa"]',
      popover: {
        title: t("tour.step.qa.title"),
        description: t("tour.step.qa.desc"),
        side: "right",
        align: "start",
      },
    },
    {
      element: '[data-tour="nav-reviews"]',
      popover: {
        title: t("tour.step.reviews.title"),
        description: t("tour.step.reviews.desc"),
        side: "right",
        align: "start",
      },
    },
    {
      element: '[data-tour="nav-agent"]',
      popover: {
        title: t("tour.step.agent.title"),
        description: t("tour.step.agent.desc"),
        side: "right",
        align: "start",
      },
    },
    {
      // No element — driver.js renders this one centred, as a closing note.
      popover: {
        title: t("tour.step.help.title"),
        description: t("tour.step.help.desc"),
      },
    },
  ];

  const tour = driver({
    ...baseConfig(t),
    steps,
    onDestroyed: () => {
      try {
        localStorage.setItem(TOUR_DONE_KEY, "1");
      } catch {
        /* private mode etc. */
      }
    },
  });
  tour.drive();
}

/**
 * One-off highlight of a single element ("show me where"). Waits briefly for
 * the target to appear — destination pages render their content only after
 * their queries resolve. Falls back to a centred popover if it never does.
 */
export function startSpotlight(el: string, title: string, description: string) {
  const show = (element?: string) =>
    // Close-only: a spotlight has no steps to navigate, and its texts arrive
    // already translated, so the footer nav buttons would show raw defaults.
    driver({ showProgress: false, showButtons: ["close"], popoverClass: "celmis-tour" }).highlight({
      element,
      popover: { title, description, side: "bottom", align: "start" },
    });

  let attempts = 12; // ~3s at 250ms
  const tick = () => {
    if (document.querySelector(el)) {
      show(el);
    } else if (--attempts > 0) {
      setTimeout(tick, 250);
    } else {
      show(undefined);
    }
  };
  tick();
}

/**
 * Destination-page half of "show me where": on mount, consume the pending
 * spotlight request (if any) and highlight the stored target. The key is
 * removed before showing so a reload never re-triggers it.
 */
export function useSpotlightOnMount() {
  useEffect(() => {
    let raw: string | null = null;
    try {
      raw = sessionStorage.getItem(SPOTLIGHT_KEY);
      if (raw) sessionStorage.removeItem(SPOTLIGHT_KEY);
    } catch {
      return; // no storage — nothing to consume
    }
    if (!raw) return;
    try {
      const { el, title, desc } = JSON.parse(raw) as SpotlightPayload;
      if (el && title) startSpotlight(el, title, desc ?? "");
    } catch {
      /* malformed payload — ignore */
    }
  }, []);
}
