"use client";

/**
 * The full platform playbook — 19 scenario cards in 5 groups, each a short
 * numbered "which buttons, in which order" recipe with a deep link.
 *
 * Extracted verbatim from the onboarding wizard: it now renders on the
 * /capabilities reference page, while onboarding links there instead of
 * inlining all 19 cards under the setup steps.
 */

import Link from "next/link";
import { useState } from "react";
import {
  ArrowRightIcon, BotIcon, BuildingIcon, GitPullRequestIcon,
  MessagesSquareIcon, ZapIcon,
} from "lucide-react";

import { useT } from "@/lib/i18n";
import { Button } from "@/components/ui/button";
import { CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SpotlightCard } from "@/components/ui/spotlight-card";

type Level = "beginner" | "advanced";
/** `level` decides which filter shows the card. "beginner" is the set someone
 *  needs to get a first result; "advanced" is everything that only matters
 *  once the basics run — policies, access rules, budgets, ops. */
type GuideCard = { key: string; href: string; nSteps: number; level: Level };
type GuideGroup = { key: string; icon: React.ReactNode; cards: GuideCard[] };

// Full platform playbook. Card copy lives in i18n as
// onboarding.g.{card}.title / .s1..sN / .cta — one card per capability.
const GUIDE_GROUPS: GuideGroup[] = [
  {
    key: "review",
    icon: <GitPullRequestIcon className="h-4 w-4" />,
    cards: [
      // auto-review toggles moved from /repositories to the Reviews page —
      // keep the CTA in sync with the updated onboarding.g.auto.s1 copy.
      { key: "auto", href: "/reviews", nSteps: 3, level: "beginner" },
      { key: "manual", href: "/reviews", nSteps: 3, level: "beginner" },
      { key: "policies", href: "/admin/review-policies", nSteps: 4, level: "advanced" },
      { key: "agents", href: "/admin/agents", nSteps: 3, level: "advanced" },
      { key: "engine", href: "/settings/llm", nSteps: 3, level: "advanced" },
      { key: "compliance", href: "/admin/compliance", nSteps: 3, level: "advanced" },
    ],
  },
  {
    key: "explore",
    icon: <MessagesSquareIcon className="h-4 w-4" />,
    cards: [
      { key: "projects", href: "/projects", nSteps: 3, level: "beginner" },
      { key: "chats", href: "/chats", nSteps: 3, level: "beginner" },
      { key: "search", href: "/search", nSteps: 2, level: "beginner" },
      { key: "intel", href: "/admin/intel", nSteps: 3, level: "advanced" },
    ],
  },
  {
    key: "automation",
    icon: <BotIcon className="h-4 w-4" />,
    cards: [
      { key: "claude", href: "/claude", nSteps: 4, level: "beginner" },
      // Six, not four: the recipe stopped at "fix it with Claude" and never
      // mentioned the SBOM or the evidence pack — the two things a customer or
      // an auditor asks for by name, and the only part of this page a
      // non-developer comes here for.
      { key: "deps", href: "/dependencies", nSteps: 6, level: "beginner" },
      { key: "alerts", href: "/alerts", nSteps: 3, level: "advanced" },
    ],
  },
  {
    key: "team",
    icon: <BuildingIcon className="h-4 w-4" />,
    cards: [
      { key: "invites", href: "/admin/workspaces", nSteps: 3, level: "beginner" },
      { key: "access", href: "/admin/access", nSteps: 3, level: "advanced" },
      { key: "budget", href: "/admin/usage", nSteps: 3, level: "advanced" },
    ],
  },
  {
    key: "ops",
    icon: <ZapIcon className="h-4 w-4" />,
    cards: [
      { key: "notifications", href: "/admin/notifications", nSteps: 3, level: "advanced" },
      { key: "jobs", href: "/admin/jobs", nSteps: 2, level: "advanced" },
      { key: "usage", href: "/settings", nSteps: 2, level: "beginner" },
    ],
  },
];

export function ScenarioCards() {
  const t = useT();
  // Nineteen cards at once is a wall. Someone setting the product up for the
  // first time needs six of them and is slowed down by the other thirteen.
  const [level, setLevel] = useState<Level | "all">("beginner");
  const groups = GUIDE_GROUPS
    .map((g) => ({ ...g, cards: g.cards.filter((c) => level === "all" || c.level === level) }))
    .filter((g) => g.cards.length > 0);
  return (
    <div className="space-y-6 pt-2">
      <div>
        <h2 className="text-lg font-semibold">{t("onboarding.sc.title")}</h2>
        <p className="text-sm text-[var(--color-muted-foreground)]">{t("onboarding.sc.subtitle")}</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {(["beginner", "advanced", "all"] as const).map((l) => (
          <Button
            key={l}
            type="button"
            size="sm"
            variant={level === l ? "default" : "outline"}
            onClick={() => setLevel(l)}
          >
            {t(`onboarding.sc.level.${l}`)}
          </Button>
        ))}
      </div>
      {groups.map((g) => (
        <section key={g.key} className="space-y-3">
          <h3 className="flex items-center gap-2 border-b border-[var(--color-border)] pb-1.5 text-sm font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)]">
            {g.icon} {t(`onboarding.gg.${g.key}`)}
          </h3>
          <div className="grid gap-3 sm:grid-cols-2">
            {g.cards.map((c) => (
              <SpotlightCard key={c.key}>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">{t(`onboarding.g.${c.key}.title`)}</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  <ol className="list-decimal space-y-1 pl-5 text-xs text-[var(--color-muted-foreground)]">
                    {Array.from({ length: c.nSteps }, (_, i) => (
                      <li key={i}>{t(`onboarding.g.${c.key}.s${i + 1}`)}</li>
                    ))}
                  </ol>
                  <Link href={c.href}>
                    <Button size="sm" variant="ghost" className="-ml-2">
                      {t(`onboarding.g.${c.key}.cta`)} <ArrowRightIcon className="ml-1 h-3 w-3" />
                    </Button>
                  </Link>
                </CardContent>
              </SpotlightCard>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
