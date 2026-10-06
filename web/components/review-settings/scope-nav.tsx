"use client";

/**
 * The left panel: Global, then every repository — searchable, each with the
 * number of settings it sets or overrides — as a tree whose branches fold.
 *
 * Every scope (Global included) unfolds into the same list of sections, each
 * with its own count, so it can be read for "where are the overrides" without
 * opening the scope. The chevron only folds; the title opens the scope (and
 * unfolds it); the open scope unfolds by itself. What is unfolded is kept per
 * person in localStorage, so the panel looks the same on the next visit.
 *
 * Every entry is a link (it opens in a new tab, it can be copied); the page
 * intercepts a click only to ask before unsaved changes are left behind.
 *
 * A repository's full name ("owner/slug") used to be the row's `title`, and
 * the bubble it raised sat over the "Per repository" heading. It is now shown
 * only when the slug is cut off, beside the panel rather than over it — see
 * `fullNameTip`.
 */

import Link from "next/link";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useSession } from "next-auth/react";
import { AnimatePresence, useReducedMotion } from "motion/react";
import * as m from "motion/react-m";
import {
  BotIcon, BrainIcon, ChevronRightIcon, FilterIcon, GlobeIcon, MessageSquareTextIcon,
  ScrollTextIcon, SearchIcon, SettingsIcon, SparklesIcon,
  TerminalIcon, TextQuoteIcon, WrenchIcon,
} from "lucide-react";

import type { ReviewSettingsOverview } from "@/lib/api";
import {
  REPO_ONLY_SECTIONS, SECTION_IDS, settingsHref, type SectionId,
} from "@/lib/review-settings-routes";
import { useT } from "@/lib/i18n";
import { useStoredString } from "@/lib/use-stored-string";
import { cn } from "@/lib/utils";
import { Checkbox } from "@/components/ui/checkbox";
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
  commands: TerminalIcon,
  learning: BrainIcon,
  advanced: WrenchIcon,
};

/** Past this many repositories the list is searched rather than scanned. */
const SEARCH_FROM = 6;

/** "Collapse all / Expand all" is offered from this many repositories. */
const BULK_FROM = 2;

/** Tree keys. "repos" is the "Per repository" group itself. */
const GLOBAL_KEY = "global";
const REPOS_KEY = "repos";
const repoKey = (slug: string) => `repo:${slug}`;

/** One indentation guide for every level: the line hangs from the centre of
 *  the chevron above it (size-7 → 14px → ml-3.5). */
const GUIDE = "ml-3.5 border-l border-[var(--color-border)] pl-1.5";

/**
 * What the panel had unfolded, read back from storage.
 *
 * Nothing stored (first visit, private mode, unreadable JSON) is the safe
 * default: the repository list open, every scope folded except the one being
 * edited — which unfolds by itself. A stored empty list is a choice, kept.
 */
export function parseExpanded(raw: string | null): Set<string> {
  if (raw !== null) {
    try {
      const v: unknown = JSON.parse(raw);
      if (Array.isArray(v)) {
        return new Set(v.filter((x): x is string => typeof x === "string").slice(0, 500));
      }
    } catch {
      /* corrupt entry — fall back to the default */
    }
  }
  return new Set(["repos"]);
}

/**
 * Is a branch unfolded? Explicitly, or because it holds the open scope
 * (`auto`) and the chevron has not folded it since that scope was opened
 * (`dismissed`).
 */
export function isScopeOpen(
  key: string,
  expanded: ReadonlySet<string>,
  auto: readonly string[],
  dismissed: readonly string[],
): boolean {
  return expanded.has(key) || (auto.includes(key) && !dismissed.includes(key));
}

export type TipBox = { top: number; left: number; maxWidth: number };

/** Pixels, from getBoundingClientRect / scrollWidth, in viewport space. */
export type TipMeasure = {
  labelScrollWidth: number;
  labelClientWidth: number;
  rowTop: number;
  rowHeight: number;
  panelRight: number;
  viewportWidth: number;
};

/**
 * Where to show a repository's full name — or null for "do not".
 *
 * Null when the slug is not cut off: the bubble would repeat what is on
 * screen. Otherwise the bubble goes to the RIGHT of the whole panel, centred
 * on the row, so it can never cover a heading or a neighbouring row — the
 * old one rose above the row and hid "Per repository". When there is no room
 * beside the panel (the stacked phone layout, where the panel is the full
 * width and slugs rarely truncate) it is null as well, rather than falling
 * back onto the text.
 */
export function fullNameTip(m: TipMeasure): TipBox | null {
  if (m.labelScrollWidth <= m.labelClientWidth) return null;
  const left = m.panelRight + 8;
  const room = m.viewportWidth - left - 12;
  if (room < 120) return null;
  return { top: m.rowTop + m.rowHeight / 2, left, maxWidth: Math.min(room, 448) };
}

function CountPill({ count, label }: { count: number; label: string }) {
  if (count <= 0) return null;
  return (
    <span className="min-w-5 shrink-0 rounded-full bg-[var(--color-attention-soft)] px-1.5 text-center text-[10px] font-semibold tabular-nums text-[var(--color-attention)]">
      <span aria-hidden>{count}</span>
      <span className="sr-only">{label}</span>
    </span>
  );
}

function Chevron({
  open, controls, label, onToggle, treeItem = false,
}: {
  open: boolean;
  controls: string;
  label: string;
  onToggle: () => void;
  /** The row's own focus stop for ↑/↓ (a group header has no title link). */
  treeItem?: boolean;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      aria-label={label}
      onClick={onToggle}
      data-tree-item={treeItem ? "" : undefined}
      className="grid size-7 shrink-0 place-items-center rounded-md text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-ring)]"
    >
      <ChevronRightIcon
        aria-hidden
        className={cn("h-3.5 w-3.5 transition-transform duration-200 motion-reduce:transition-none", open && "rotate-90")}
      />
    </button>
  );
}

/** Height-animated fold. Reduced motion: it snaps. */
function Collapse({ open, id, children }: { open: boolean; id: string; children: React.ReactNode }) {
  const reduce = useReducedMotion();
  return (
    <AnimatePresence initial={false}>
      {open && (
        <m.div
          id={id}
          key="body"
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={reduce ? { duration: 0 } : { duration: 0.2, ease: [0.23, 1, 0.32, 1] }}
          className="overflow-hidden"
        >
          {children}
        </m.div>
      )}
    </AnimatePresence>
  );
}

const ROW = "flex items-center gap-0.5 rounded-md pr-1 transition-colors";
const ROW_ACTIVE = "bg-[var(--color-selected)] text-[var(--color-selected-foreground)]";
const ROW_IDLE = "hover:bg-[var(--color-accent)]";
const ROW_LINK = "flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-md py-1.5 pl-1 pr-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-ring)] lg:min-h-8";

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
  const uid = useId();
  const navRef = useRef<HTMLElement | null>(null);
  const { data: session } = useSession();
  const [q, setQ] = useState("");
  const [onlyOverridden, setOnlyOverridden] = useState(false);

  // ── what is unfolded ──
  const [stored, store] = useStoredString(
    `celmis:review-settings:nav:${session?.user?.id ?? "anon"}`,
  );
  const expanded = useMemo(() => parseExpanded(stored), [stored]);
  const isGlobal = scope.kind === "workspace";
  const activeKey = scope.kind === "repo" ? repoKey(scope.slug) : GLOBAL_KEY;
  const auto = isGlobal ? [GLOBAL_KEY] : [REPOS_KEY, activeKey];
  // Folded by the chevron while this scope is open; forgotten on leaving it,
  // so coming back unfolds it again.
  const [dismissedFor, setDismissedFor] = useState<{ scope: string; keys: string[] }>({ scope: "", keys: [] });
  const dismissed = dismissedFor.scope === activeKey ? dismissedFor.keys : [];
  const isOpen = (key: string) => isScopeOpen(key, expanded, auto, dismissed);

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

  const save = (next: Set<string>, nextDismissed: string[]) => {
    // Repositories that are gone are not carried forward forever.
    const known = overview ? new Set(overview.repositories.map((r) => repoKey(r.repo_slug))) : null;
    store(JSON.stringify([...next].filter((k) => !k.startsWith("repo:") || !known || known.has(k))));
    setDismissedFor({ scope: activeKey, keys: nextDismissed });
  };
  const setOpen = (key: string, open: boolean) => {
    const next = new Set(expanded);
    if (open) next.add(key);
    else next.delete(key);
    const rest = dismissed.filter((k) => k !== key);
    save(next, !open && auto.includes(key) ? [...rest, key] : rest);
  };
  const toggle = (key: string) => setOpen(key, !isOpen(key));

  const repoKeys = repos.map((r) => repoKey(r.repo_slug));
  const anyRepoOpen = repoKeys.some(isOpen);
  const toggleAllRepos = () => {
    const next = new Set(expanded);
    if (anyRepoOpen) {
      for (const k of [...next]) if (k.startsWith("repo:")) next.delete(k);
      save(next, scope.kind === "repo" ? [...new Set([...dismissed, activeKey])] : dismissed);
    } else {
      for (const k of repoKeys) next.add(k);
      save(next, dismissed.filter((k) => k !== activeKey));
    }
  };

  // ── the full-name bubble ──
  const tipId = `${uid}-fullname`;
  const [tip, setTip] = useState<(TipBox & { slug: string; text: string }) | null>(null);
  useEffect(() => {
    if (!tip) return;
    // The bubble is placed in viewport coordinates; once the panel or the
    // page scrolls it would point at the wrong row.
    const hide = () => setTip(null);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, [tip]);
  const showTip = (
    e: React.PointerEvent<HTMLAnchorElement> | React.FocusEvent<HTMLAnchorElement>,
    slug: string,
    text: string,
  ) => {
    if ("pointerType" in e && e.pointerType === "touch") return;
    const label = e.currentTarget.querySelector<HTMLElement>("[data-label]");
    const row = e.currentTarget.parentElement?.getBoundingClientRect();
    const nav = navRef.current;
    const panel = (nav?.closest("aside") ?? nav)?.getBoundingClientRect();
    if (!label || !row || !panel) return;
    const box = fullNameTip({
      labelScrollWidth: label.scrollWidth,
      labelClientWidth: label.clientWidth,
      rowTop: row.top,
      rowHeight: row.height,
      panelRight: panel.right,
      viewportWidth: window.innerWidth,
    });
    setTip(box ? { ...box, slug, text } : null);
  };
  const hideTip = () => setTip(null);

  // ── keyboard: tree semantics on top of ordinary links and buttons ──
  const onTreeKeyDown = (e: React.KeyboardEvent<HTMLUListElement>) => {
    const target = e.target as HTMLElement;
    if (target.closest("input, textarea, select")) return;
    const item = target.closest<HTMLElement>('[role="treeitem"]');
    if (!item) return;
    const key = item.dataset.key;
    const expandable = item.hasAttribute("aria-expanded");
    const open = item.getAttribute("aria-expanded") === "true";
    const own = item.querySelector<HTMLElement>("[data-tree-item]");
    switch (e.key) {
      case "ArrowRight":
        if (!expandable || !key) return;
        e.preventDefault();
        if (!open) setOpen(key, true);
        else item.querySelector<HTMLElement>('[role="group"] [data-tree-item]')?.focus();
        return;
      case "ArrowLeft":
        e.preventDefault();
        if (expandable && open && key) setOpen(key, false);
        else item.parentElement?.closest('[role="treeitem"]')
          ?.querySelector<HTMLElement>("[data-tree-item]")?.focus();
        return;
      case "ArrowDown":
      case "ArrowUp":
      case "Home":
      case "End": {
        const all = [...e.currentTarget.querySelectorAll<HTMLElement>("[data-tree-item]")];
        if (!all.length) return;
        e.preventDefault();
        const here = all.indexOf(target.closest<HTMLElement>("[data-tree-item]") ?? own ?? target);
        const to = e.key === "Home" ? 0
          : e.key === "End" ? all.length - 1
          : e.key === "ArrowDown" ? Math.min(all.length - 1, here + 1)
          : Math.max(0, here - 1);
        all[to]?.focus();
        return;
      }
    }
  };

  const sectionList = (
    repo: string | null,
    counts: Map<SectionId, number>,
    active: boolean,
    level: number,
  ) => (
    <ul role="group" className={cn("mt-0.5 space-y-px pb-1", GUIDE)}>
      {SECTION_IDS.filter((s) => repo || !REPO_ONLY_SECTIONS.includes(s)).map((s) => {
        const Icon = SECTION_ICON[s];
        const current = active && s === section;
        const count = counts.get(s) ?? 0;
        const dirty = active ? changed.get(s) ?? 0 : 0;
        const href = settingsHref({ repo, section: s });
        return (
          <li key={s} role="treeitem" aria-level={level} aria-selected={current}>
            <Link
              href={href}
              data-tree-item=""
              aria-current={current ? "page" : undefined}
              onClick={(e) => onNavigate(e, href, active)}
              className={cn(
                "group flex min-h-9 items-center gap-2 rounded-md px-2 py-1.5 text-[13px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-ring)] lg:min-h-8",
                current
                  ? "bg-[var(--color-selected)] font-medium text-[var(--color-selected-foreground)]"
                  : "text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]",
              )}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
              <span className="min-w-0 flex-1 truncate">{t(`reviewSettings.section.${s}`)}</span>
              {dirty > 0 && (
                <span
                  className="size-1.5 shrink-0 rounded-full bg-[var(--color-primary)]"
                  title={t("reviewSettings.save.unsavedShort")}
                >
                  <span className="sr-only">{t("reviewSettings.save.unsavedShort")}</span>
                </span>
              )}
              <CountPill
                count={count}
                label={repo
                  ? t("reviewSettings.nav.overriddenCount", { count })
                  : t("reviewSettings.nav.setCount", { count })}
              />
            </Link>
          </li>
        );
      })}
    </ul>
  );

  const sum = (counts: Map<SectionId, number>) => [...counts.values()].reduce((a, b) => a + b, 0);
  const globalHref = settingsHref({ section: REPO_ONLY_SECTIONS.includes(section) ? null : section });
  const globalCounts = isGlobal ? activeCounts : sectionsOfFields(overview?.workspace.set_fields ?? []);
  const globalCount = isGlobal ? sum(activeCounts) : overview?.workspace.set_count ?? 0;
  const globalOpen = isOpen(GLOBAL_KEY);
  const globalListId = `${uid}-global`;
  const reposOpen = isOpen(REPOS_KEY);
  const reposGroupId = `${uid}-repos`;

  return (
    <nav ref={navRef} aria-label={t("reviewSettings.nav.label")} className="text-sm">
      <ul role="tree" aria-label={t("reviewSettings.nav.label")} onKeyDown={onTreeKeyDown} className="space-y-4">
        <li role="treeitem" aria-level={1} aria-expanded={globalOpen} aria-selected={isGlobal} data-key={GLOBAL_KEY}>
          <div className={cn(ROW, isGlobal ? ROW_ACTIVE : ROW_IDLE)}>
            <Chevron
              open={globalOpen}
              controls={globalListId}
              label={t("reviewSettings.nav.sectionsOf", { scope: t("reviewSettings.scope.global") })}
              onToggle={() => toggle(GLOBAL_KEY)}
            />
            <Link
              href={globalHref}
              data-tree-item=""
              aria-current={isGlobal ? "location" : undefined}
              onClick={(e) => {
                onNavigate(e, globalHref, isGlobal);
                if (isGlobal) setOpen(GLOBAL_KEY, true);
              }}
              className={ROW_LINK}
            >
              <GlobeIcon className="h-4 w-4 shrink-0 text-[var(--color-primary)]" aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{t("reviewSettings.scope.global")}</span>
                <span className="block truncate text-xs text-[var(--color-muted-foreground)]">
                  {t("reviewSettings.scope.globalHint")}
                </span>
              </span>
              <CountPill count={globalCount} label={t("reviewSettings.nav.setCount", { count: globalCount })} />
            </Link>
          </div>
          <Collapse open={globalOpen} id={globalListId}>
            {sectionList(null, globalCounts, isGlobal, 2)}
          </Collapse>
        </li>

        <li role="treeitem" aria-level={1} aria-expanded={reposOpen} aria-selected={false} data-key={REPOS_KEY}>
          <div className="flex items-start gap-0.5 pr-1">
            <Chevron
              open={reposOpen}
              controls={reposGroupId}
              label={t("reviewSettings.nav.repoList")}
              onToggle={() => toggle(REPOS_KEY)}
              treeItem
            />
            <div className="min-w-0 flex-1 pl-1 pt-1">
              <div className="flex items-center gap-2">
                <h2 className="min-w-0 flex-1 truncate font-medium">{t("reviewSettings.scope.perRepo")}</h2>
                {reposOpen && repos.length >= BULK_FROM && (
                  <button
                    type="button"
                    onClick={toggleAllRepos}
                    aria-controls="review-settings-repos"
                    className="shrink-0 rounded px-1.5 py-0.5 text-xs text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)]"
                  >
                    {anyRepoOpen ? t("reviewSettings.nav.collapseAll") : t("reviewSettings.nav.expandAll")}
                  </button>
                )}
              </div>
              <p className="text-xs text-[var(--color-muted-foreground)]">{t("reviewSettings.scope.perRepoHint")}</p>
            </div>
          </div>
          <Collapse open={reposOpen} id={reposGroupId}>
            <div className={cn("mt-2 space-y-2 pb-0.5 pr-1 pt-0.5", GUIDE)}>
              {total >= SEARCH_FROM && (
                <div className="relative">
                  <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--color-muted-foreground)]" aria-hidden />
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
                <label className="flex cursor-pointer items-center gap-2 px-1 text-xs text-[var(--color-muted-foreground)]">
                  <Checkbox
                    checked={onlyOverridden}
                    onChange={(e) => setOnlyOverridden(e.target.checked)}
                  />
                  {t("reviewSettings.nav.onlyOverridden", { count: withOverrides })}
                </label>
              )}
              {loading && (
                <div className="space-y-2 px-1" aria-hidden>
                  {[0, 1, 2].map((i) => <Skeleton key={i} className="h-8 w-full" />)}
                </div>
              )}
              {!loading && total === 0 && (
                <p className="px-1 text-xs text-[var(--color-muted-foreground)]">
                  {t("reviewSettings.nav.noRepos")}{" "}
                  <Link href="/repositories" className="font-medium text-[var(--color-primary)] underline-offset-4 hover:underline">
                    {t("nav.repositories")}
                  </Link>
                </p>
              )}
              {!loading && total > 0 && repos.length === 0 && (
                <p className="px-1 text-xs text-[var(--color-muted-foreground)]">{t("reviewSettings.nav.noMatches")}</p>
              )}
              <ul role="group" id="review-settings-repos" className="space-y-px">
                {repos.map((r) => {
                  const key = repoKey(r.repo_slug);
                  const active = scope.kind === "repo" && scope.slug === r.repo_slug;
                  const open = isOpen(key);
                  const href = settingsHref({ repo: r.repo_slug, section });
                  const count = active ? sum(activeCounts) : r.overridden_count;
                  const listId = `${uid}-repo-${r.repo_slug}`;
                  return (
                    <li
                      key={r.repo_slug}
                      role="treeitem"
                      aria-level={2}
                      aria-expanded={open}
                      aria-selected={active}
                      data-key={key}
                    >
                      <div className={cn(ROW, active ? ROW_ACTIVE : ROW_IDLE)}>
                        <Chevron
                          open={open}
                          controls={listId}
                          label={t("reviewSettings.nav.sectionsOf", { scope: r.repo_slug })}
                          onToggle={() => toggle(key)}
                        />
                        <Link
                          href={href}
                          data-tree-item=""
                          aria-current={active ? "location" : undefined}
                          aria-describedby={tip?.slug === r.repo_slug ? tipId : undefined}
                          onClick={(e) => {
                            hideTip();
                            onNavigate(e, href, active);
                            if (active) setOpen(key, true);
                          }}
                          onPointerEnter={(e) => showTip(e, r.repo_slug, r.full_name)}
                          onPointerLeave={hideTip}
                          onFocus={(e) => showTip(e, r.repo_slug, r.full_name)}
                          onBlur={hideTip}
                          className={ROW_LINK}
                        >
                          <span
                            data-label=""
                            className={cn("min-w-0 flex-1 truncate font-mono text-[12.5px]", active && "font-medium")}
                          >
                            {r.repo_slug}
                          </span>
                          {!r.review_enabled && (
                            <span className="shrink-0 text-[10px] text-[var(--color-muted-foreground)]">
                              {t("reviewSettings.nav.reviewOff")}
                            </span>
                          )}
                          <CountPill count={count} label={t("reviewSettings.nav.overriddenCount", { count })} />
                        </Link>
                      </div>
                      <Collapse open={open} id={listId}>
                        {sectionList(r.repo_slug, active ? activeCounts : sectionsOfFields(r.overridden_fields), active, 3)}
                      </Collapse>
                    </li>
                  );
                })}
              </ul>
            </div>
          </Collapse>
        </li>
      </ul>

      {tip && typeof document !== "undefined" && createPortal(
        // Outer box placed and centred by CSS, inner one animated — Motion's
        // `x` on the same box would overwrite the centring translate.
        <span
          style={{ position: "fixed", top: tip.top, left: tip.left, maxWidth: tip.maxWidth }}
          className="pointer-events-none z-50 -translate-y-1/2"
        >
          <m.span
            id={tipId}
            role="tooltip"
            initial={{ opacity: 0, x: -4 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.14, ease: [0.2, 0, 0, 1] }}
            className="block break-all rounded-md border border-[var(--color-border)] bg-[var(--color-popover)] px-2 py-1 font-mono text-xs text-[var(--color-popover-foreground)] shadow-[var(--shadow-md)]"
          >
            {tip.text}
          </m.span>
        </span>,
        document.body,
      )}
    </nav>
  );
}
