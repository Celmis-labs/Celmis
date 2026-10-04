"use client";

/**
 * The left panel: Global, then every repository — searchable, each with the
 * number of settings it overrides — and, under the open scope, its sections.
 *
 * Every entry is a link (it opens in a new tab, it can be copied); the page
 * intercepts a click only to ask before unsaved changes are left behind.
 * A repository's sections can be unfolded without opening it, to see where
 * its overrides are before going there.
 */

import Link from "next/link";
import { useMemo, useState } from "react";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import {
  BotIcon, ChevronRightIcon, FilterIcon, GlobeIcon, MessageSquareTextIcon,
  ScrollTextIcon, SearchIcon, SettingsIcon, SparklesIcon,
  TextQuoteIcon, WrenchIcon,
} from "lucide-react";

import type { ReviewSettingsOverview } from "@/lib/api";
import {
  REPO_ONLY_SECTIONS, SECTION_IDS, settingsHref, type SectionId,
} from "@/lib/review-settings-routes";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import type { Scope } from "@/components/review-settings/model";
import { sectionsOfFields } from "@/components/review-settings/model";

export const SECTION_ICON: Record<SectionId, typeof SettingsIcon> = {
  general: SettingsIcon,
  categories: BotIcon,
  filters: FilterIcon,
  prompts: SparklesIcon,
  summary: TextQuoteIcon,
  rules: ScrollTextIcon,
  messages: MessageSquareTextIcon,
  advanced: WrenchIcon,
};

/** Past this many repositories the list is searched rather than scanned. */
const SEARCH_FROM = 6;

export function ScopeNav({
  overview, loading, scope, section, activeCounts, changed, onNavigate,
}: {
  overview: ReviewSettingsOverview | undefined;
  loading: boolean;
  scope: Scope;
  section: SectionId;
  /** Settings the open scope sets, per section (from the draft). */
  activeCounts: Map<SectionId, number>;
  /** Unsaved changes in the open scope, per section. */
  changed: Map<SectionId, number>;
  onNavigate: (e: React.MouseEvent, href: string, sameScope: boolean) => void;
}) {
  const t = useT();
  const [q, setQ] = useState("");
  const [onlyOverridden, setOnlyOverridden] = useState(false);
  const [unfolded, setUnfolded] = useState<Set<string>>(new Set());
  const repos = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return [...(overview?.repositories ?? [])]
      .filter((r) => !needle
        || r.repo_slug.toLowerCase().includes(needle)
        || r.full_name.toLowerCase().includes(needle))
      .filter((r) => !onlyOverridden || r.overridden_count > 0)
      .sort((a, b) => a.repo_slug.localeCompare(b.repo_slug));
  }, [overview, q, onlyOverridden]);
  const total = overview?.repositories.length ?? 0;
  const withOverrides = overview?.repositories.filter((r) => r.overridden_count > 0).length ?? 0;
  const isGlobal = scope.kind === "workspace";

  const sectionList = (repo: string | null, counts: Map<SectionId, number>, active: boolean) => (
    <ul className="mt-0.5 space-y-px border-l border-[var(--color-border)] pl-2 ml-[1.15rem]">
      {SECTION_IDS.filter((s) => repo || !REPO_ONLY_SECTIONS.includes(s)).map((s) => {
        const Icon = SECTION_ICON[s];
        const current = active && s === section;
        const count = counts.get(s) ?? 0;
        const dirty = active ? changed.get(s) ?? 0 : 0;
        const href = settingsHref({ repo, section: s });
        return (
          <li key={s}>
            <Link
              href={href}
              aria-current={current ? "page" : undefined}
              onClick={(e) => onNavigate(e, href, active)}
              className={cn(
                "group flex min-h-9 items-center gap-2 rounded-md px-2 py-1.5 text-[13px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] lg:min-h-8",
                current
                  ? "bg-[var(--color-brand-muted)] font-medium text-[var(--color-brand)]"
                  : "text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]",
              )}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
              <span className="min-w-0 flex-1 truncate">{t(`reviewSettings.section.${s}`)}</span>
              {dirty > 0 && (
                <span
                  className="size-1.5 shrink-0 rounded-full bg-[var(--color-brand)]"
                  title={t("reviewSettings.save.unsavedShort")}
                >
                  <span className="sr-only">{t("reviewSettings.save.unsavedShort")}</span>
                </span>
              )}
              {count > 0 && (
                <span
                  className={cn(
                    "min-w-5 rounded-full px-1.5 text-center text-[10px] font-semibold tabular-nums",
                    repo
                      ? "bg-[var(--color-warning)]/20 text-[var(--color-warning)]"
                      : "bg-[var(--color-brand-muted)] text-[var(--color-brand)]",
                  )}
                >
                  <span aria-hidden>{count}</span>
                  <span className="sr-only">
                    {repo
                      ? t("reviewSettings.nav.overriddenCount", { count })
                      : t("reviewSettings.nav.setCount", { count })}
                  </span>
                </span>
              )}
            </Link>
          </li>
        );
      })}
    </ul>
  );

  return (
    <nav aria-label={t("reviewSettings.nav.label")} className="space-y-5 text-sm">
      <div>
        <Link
          href={settingsHref({ section: REPO_ONLY_SECTIONS.includes(section) ? null : section })}
          aria-current={isGlobal ? "location" : undefined}
          onClick={(e) => onNavigate(e, settingsHref({ section: REPO_ONLY_SECTIONS.includes(section) ? null : section }), isGlobal)}
          className={cn(
            "flex items-center gap-2 rounded-md px-2 py-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]",
            isGlobal ? "bg-[var(--color-accent)] text-[var(--color-foreground)]" : "hover:bg-[var(--color-accent)]",
          )}
        >
          <GlobeIcon className="h-4 w-4 shrink-0 text-[var(--color-brand)]" aria-hidden />
          <span className="min-w-0 flex-1">
            <span className="block font-medium">{t("reviewSettings.scope.global")}</span>
            <span className="block text-xs text-[var(--color-muted-foreground)]">
              {t("reviewSettings.scope.globalHint")}
            </span>
          </span>
          {(overview?.workspace.set_count ?? 0) > 0 && (
            <span className="rounded-full bg-[var(--color-brand-muted)] px-1.5 text-[10px] font-semibold tabular-nums text-[var(--color-brand)]">
              <span aria-hidden>{overview?.workspace.set_count}</span>
              <span className="sr-only">
                {t("reviewSettings.nav.setCount", { count: overview?.workspace.set_count ?? 0 })}
              </span>
            </span>
          )}
        </Link>
        {isGlobal && sectionList(null, activeCounts, true)}
      </div>

      <div className="space-y-2">
        <div className="px-2">
          <h2 className="font-medium">{t("reviewSettings.scope.perRepo")}</h2>
          <p className="text-xs text-[var(--color-muted-foreground)]">{t("reviewSettings.scope.perRepoHint")}</p>
        </div>
        {total >= SEARCH_FROM && (
          <div className="relative px-1">
            <SearchIcon className="pointer-events-none absolute left-3.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--color-muted-foreground)]" aria-hidden />
            <Input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("reviewSettings.nav.search")}
              aria-label={t("reviewSettings.nav.search")}
              aria-controls="review-settings-repos"
              className="h-9 pl-8 text-sm"
            />
          </div>
        )}
        {withOverrides > 0 && total >= SEARCH_FROM && (
          <label className="flex cursor-pointer items-center gap-2 px-2 text-xs text-[var(--color-muted-foreground)]">
            <input
              type="checkbox"
              checked={onlyOverridden}
              onChange={(e) => setOnlyOverridden(e.target.checked)}
              className="accent-[var(--color-brand)]"
            />
            {t("reviewSettings.nav.onlyOverridden", { count: withOverrides })}
          </label>
        )}
        {loading && (
          <div className="space-y-2 px-2" aria-hidden>
            {[0, 1, 2].map((i) => <Skeleton key={i} className="h-8 w-full" />)}
          </div>
        )}
        {!loading && total === 0 && (
          <p className="px-2 text-xs text-[var(--color-muted-foreground)]">
            {t("reviewSettings.nav.noRepos")}{" "}
            <Link href="/repositories" className="font-medium text-[var(--color-brand)] underline-offset-4 hover:underline">
              {t("nav.repositories")}
            </Link>
          </p>
        )}
        {!loading && total > 0 && repos.length === 0 && (
          <p className="px-2 text-xs text-[var(--color-muted-foreground)]">{t("reviewSettings.nav.noMatches")}</p>
        )}
        <ul id="review-settings-repos" className="space-y-px">
          {repos.map((r) => {
            const active = scope.kind === "repo" && scope.slug === r.repo_slug;
            const open = active || unfolded.has(r.repo_slug);
            const href = settingsHref({ repo: r.repo_slug, section });
            const count = active
              ? [...activeCounts.values()].reduce((a, b) => a + b, 0)
              : r.overridden_count;
            const listId = `repo-sections-${r.repo_slug}`;
            return (
              <li key={r.repo_slug}>
                <div
                  className={cn(
                    "flex items-center gap-1 rounded-md pr-1 transition-colors",
                    active ? "bg-[var(--color-accent)]" : "hover:bg-[var(--color-accent)]",
                  )}
                >
                  <button
                    type="button"
                    aria-expanded={open}
                    aria-controls={listId}
                    disabled={active}
                    aria-label={t("reviewSettings.nav.toggleSections", { repo: r.repo_slug })}
                    onClick={() => setUnfolded((prev) => {
                      const next = new Set(prev);
                      if (next.has(r.repo_slug)) next.delete(r.repo_slug);
                      else next.add(r.repo_slug);
                      return next;
                    })}
                    className="grid size-8 shrink-0 place-items-center rounded-md text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] disabled:pointer-events-none"
                  >
                    <ChevronRightIcon
                      aria-hidden
                      className={cn("h-3.5 w-3.5 transition-transform duration-200", open && "rotate-90")}
                    />
                  </button>
                  <Link
                    href={href}
                    aria-current={active ? "location" : undefined}
                    onClick={(e) => onNavigate(e, href, active)}
                    title={r.full_name}
                    className="flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-md py-1.5 pr-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] lg:min-h-8"
                  >
                    <span className={cn("min-w-0 flex-1 truncate font-mono text-[12.5px]", active && "font-medium")}>
                      {r.repo_slug}
                    </span>
                    {!r.review_enabled && (
                      <span className="shrink-0 text-[10px] text-[var(--color-muted-foreground)]">
                        {t("reviewSettings.nav.reviewOff")}
                      </span>
                    )}
                    {count > 0 && (
                      <span className="shrink-0 rounded-full bg-[var(--color-warning)]/20 px-1.5 text-[10px] font-semibold tabular-nums text-[var(--color-warning)]">
                        <span aria-hidden>{count}</span>
                        <span className="sr-only">{t("reviewSettings.nav.overriddenCount", { count })}</span>
                      </span>
                    )}
                  </Link>
                </div>
                <AnimatePresence initial={false}>
                  {open && (
                    <m.div
                      id={listId}
                      key="sections"
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.18, ease: [0.23, 1, 0.32, 1] }}
                      className="overflow-hidden"
                    >
                      {sectionList(r.repo_slug, active ? activeCounts : sectionsOfFields(r.overridden_fields), active)}
                    </m.div>
                  )}
                </AnimatePresence>
              </li>
            );
          })}
        </ul>
      </div>
    </nav>
  );
}
