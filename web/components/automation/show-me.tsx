"use client";

/**
 * "Show me": the button under an answer that walks the person to the control
 * the answer is about — opens the page, scrolls to the field, dims the rest
 * and explains it in a small popover (lib/guide-tour.ts, lib/guide-run.ts).
 *
 * driver.js is loaded on the first press, not with the chat: most answers are
 * never shown this way.
 */

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { CrosshairIcon } from "lucide-react";

import { useT } from "@/lib/i18n";
import type { GuideStep } from "@/lib/guide-tour";

export function ShowMe({ steps }: { steps: GuideStep[] }) {
  const t = useT();
  const router = useRouter();
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const start = async () => {
    const { runGuide } = await import("@/lib/guide-run");
    if (!alive.current) return;
    await runGuide(steps, { t, navigate: (route) => router.push(route) });
  };

  return (
    <button
      type="button"
      onClick={() => { void start(); }}
      className="inline-flex items-center gap-1.5 rounded-full border border-[var(--color-brand)]/40 bg-[var(--color-brand)]/10 px-3 py-1.5 text-xs font-medium text-[var(--color-brand)] transition-colors hover:bg-[var(--color-brand)]/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]"
    >
      <CrosshairIcon className="h-3.5 w-3.5" aria-hidden />
      {t("guide.showMe")}
    </button>
  );
}
