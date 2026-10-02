"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { usePathname } from "next/navigation";
import { signOut, useSession } from "next-auth/react";
import { AnimatePresence, useReducedMotion } from "motion/react";
import * as m from "motion/react-m";
import { ActivityIcon, BotIcon, BuildingIcon, GaugeIcon, CheckIcon, ChevronDownIcon, ChevronRightIcon, FolderGit2Icon, GitPullRequestIcon, LayoutDashboardIcon, LogOutIcon, MessagesSquareIcon, PanelLeftIcon, PlusIcon, SettingsIcon, ShieldIcon, UsersIcon } from "lucide-react";
import { toast } from "sonner";
import { AgentWidget } from "@/components/agent-widget";
import { BrandMark, BrandWord } from "@/components/brand-mark";
import { SECTION_TABS, type TabDef } from "@/components/section-tabs";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { FloatingTooltip } from "@/components/ui/tooltip";
import { LanguageSwitcher } from "@/components/language-switcher";
import { ThemeToggle } from "@/components/theme-toggle";
import { API_BASE } from "@/lib/api";
import { forgetAgentSession } from "@/lib/agent-session";
import { LEGACY_SIDEBAR_KEY, writeSidebarCookie } from "@/lib/sidebar";
import { useT } from "@/lib/i18n";
import { startMainTour, TOUR_DONE_KEY } from "@/lib/tour";
import { cn } from "@/lib/utils";
import { LicenseFooter } from "@/components/license-footer";

// Flat top-level navigation — one row per section, no collapsible groups.
// Every sub-route of a section lives in `pages` (shared with the SectionTabs
// rows on the pages themselves), which drives both the active state and the
// breadcrumb. URLs did not move — tabs and nav are links to existing routes.
type NavSection = {
  href: string;
  labelKey: string;
  icon: typeof LayoutDashboardIcon;
  /** Visible only to global admins (session.isAdmin). */
  adminOnly?: boolean;
  /** All routes that belong to this section (prefix-matched unless exact). */
  pages: readonly TabDef[];
};

const NAV_SECTIONS: NavSection[] = [
  {
    href: "/dashboard", labelKey: "nav.dashboard", icon: LayoutDashboardIcon,
    pages: SECTION_TABS.dashboard,
  },
  { href: "/repositories", labelKey: "nav.repositories", icon: FolderGit2Icon, pages: SECTION_TABS.sources },
  { href: "/reviews", labelKey: "nav.codeReview", icon: GitPullRequestIcon, pages: SECTION_TABS.review },
  { href: "/projects", labelKey: "nav.qa", icon: MessagesSquareIcon, pages: SECTION_TABS.qa },
  { href: "/claude", labelKey: "nav.agent", icon: BotIcon, pages: SECTION_TABS.agent },
  // The Celmis agent has no row here any more. It is the round button in the
  // bottom-right corner of every page (components/agent-widget.tsx), which
  // opens it over whatever you were looking at; its full view, /automation,
  // is one press from that panel and is named by the breadcrumb through
  // UNLISTED_PAGES below.
  // Alerts, the channels that deliver them, the job queue, the audit trail,
  // and the server log tail — one section, because "is it working, and what
  // did it just do" is one question. The last two arrived here from
  // Administration as their rows learned which workspace they belong to; this
  // entry is deliberately NOT adminOnly, so a workspace owner can reach them.
  { href: "/alerts", labelKey: "nav.monitoring", icon: ActivityIcon, pages: SECTION_TABS.monitoring },
  // Not adminOnly, and that is the point. The spend endpoint has always been
  // workspace-scoped — any member reads their OWN workspace's figures, and
  // only the budget cap is an admin write. The page was nevertheless buried
  // in the global-admin section, so the person who pays for a workspace
  // could not see what it costs. A number nobody can find is not reported.
  //
  // Its own page list, not Administration's. `pages: []` matched nothing, so
  // the section that DID claim /admin/usage — the global-admin one — answered
  // for it: the breadcrumb read «Administration > Usage & cost», the
  // Administration entry lit up while you were reading your own bill, and the
  // tab row beside it offered Job queue / System status / Audit log. See the
  // `usage` key in SECTION_TABS.
  { href: "/admin/usage", labelKey: "nav.usage", icon: GaugeIcon, pages: SECTION_TABS.usage },
  { href: "/admin/workspaces", labelKey: "nav.team", icon: UsersIcon, pages: SECTION_TABS.team },
  { href: "/settings", labelKey: "nav.settings", icon: SettingsIcon, pages: SECTION_TABS.settings },
  { href: "/admin/health", labelKey: "nav.adminSection", icon: ShieldIcon, adminOnly: true, pages: SECTION_TABS.admin },
];

// Tour anchors on individual nav items, keyed by section href. Applied to both
// the expanded links and the collapsed rail so the tour finds its targets in
// either sidebar state.
const NAV_TOUR: Record<string, string> = {
  "/repositories": "nav-repos",
  "/projects": "nav-qa",
  "/reviews": "nav-reviews",
  "/claude": "nav-agent",
};

function WorkspaceSwitcher() {
  const { data } = useSession();
  const t = useT();
  const [me, setMe] = useState<any>(null);
  const jwt = data?.celmisToken;
  useEffect(() => {
    if (!jwt) return;
    fetch(`${API_BASE}/api/workspaces`, {
      headers: { Authorization: `Bearer ${jwt}` },
      credentials: "include",
    })
      .then((r) => (r.ok ? r.json() : null))
      .then(setMe)
      .catch(() => setMe(null));
  }, [jwt]);
  const active = me?.workspaces?.find((w: any) => w.id === me?.active_id);
  const list = me?.workspaces || [];
  const [open, setOpen] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);

  // Close on any pointer down outside the control.
  //
  // This replaced a `fixed inset-0` click-away layer, which was broken on
  // touch for a reason that is invisible from the markup: the top bar has
  // `backdrop-blur`, and `backdrop-filter` makes an element the CONTAINING
  // BLOCK for fixed-position descendants. So `inset-0` covered the 44px bar
  // rather than the viewport — landing exactly on the button that had just
  // been tapped. One tap opened the menu and the same gesture's follow-up
  // event hit the layer and closed it again, which from outside is
  // indistinguishable from "the menu does not open".
  //
  // A document listener has no containing block to be trapped by, and
  // `pointerdown` fires before the synthetic click that caused the trouble.
  //
  // These two MUST stay above the `!jwt` return below. They sat under it and
  // took down every authenticated page: the first render happens while
  // useSession is still loading, so `jwt` is undefined and the component
  // returns early having called eight hooks; when the session arrives the
  // early return is skipped and ten run. React counts hooks, sees more than
  // last time, and throws #310 — which unmounts the whole tree into "This
  // page couldn't load". Nothing about the placement looks wrong, and the
  // guard tests grepped the file rather than rendering it, so it shipped.
  const boxRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Where to paint the panel. It is rendered into document.body rather than
  // beside the button, so the coordinates have to be measured.
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const measure = () => {
    const r = boxRef.current?.getBoundingClientRect();
    if (r) setAnchor({ top: r.bottom + 4, right: window.innerWidth - r.right });
  };
  useEffect(() => {
    if (!open) return;
    measure();
    // The bar is sticky, so the button moves under the page: follow it rather
    // than leaving the panel behind. Passive — this never calls preventDefault.
    const onMove = () => measure();
    window.addEventListener("scroll", onMove, { passive: true, capture: true });
    window.addEventListener("resize", onMove, { passive: true });
    return () => {
      window.removeEventListener("scroll", onMove, true);
      window.removeEventListener("resize", onMove);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node;
      // Both nodes: the panel is a portal child of <body>, so it is NOT inside
      // boxRef any more. Testing only the button would close the menu on the
      // very gesture that picks a workspace from it.
      if (boxRef.current?.contains(target)) return;
      if (menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    // Capture phase: a menu item that stops propagation must not also stop
    // the outside-click detection for the next tap.
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [open]);

  if (!jwt) return null;

  const switchWs = (slug: string, wsName: string) => {
    document.cookie = `x-workspace=${slug}; path=/; max-age=31536000; SameSite=Lax`;
    // The agent's sitting belongs to the workspace it was started in.
    forgetAgentSession();
    // Picked up by AppShell after the reload — confirms the switch worked.
    try { sessionStorage.setItem("ws-switched", wsName); } catch {}
    location.reload();
  };
  const slugify = (v: string) =>
    v.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");

  const createWs = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setCreating(true);
    try {
      const r = await fetch(`${API_BASE}/api/workspaces`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${jwt}` },
        body: JSON.stringify({ name: trimmed, slug: slugify(trimmed), description: "" }),
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        toast.error(
          r.status === 403
            ? t("shell.createForbidden")
            : t("shell.createFailed", { detail: b.detail || r.status }),
        );
        return;
      }
      setDialogOpen(false);
      switchWs(slugify(trimmed), trimmed);
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="relative" ref={boxRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        data-tour="workspace"
        title={t("shell.workspace")}
        className="flex min-h-9 max-w-[45vw] items-center gap-1.5 rounded-md border border-[var(--color-border)] px-2 py-1 text-xs font-medium transition-colors hover:bg-[var(--color-accent)] sm:min-h-0 sm:max-w-none"
      >
        <BuildingIcon className="h-3.5 w-3.5 shrink-0 text-[var(--color-muted-foreground)]" />
        <span className="max-w-40 truncate">{active?.name || "default"}</span>
        {active?.role && (
          <span className="font-normal text-[10px] text-[var(--color-muted-foreground)]">
            ({active.role})
          </span>
        )}
        <ChevronDownIcon
          className={cn("h-3 w-3 shrink-0 text-[var(--color-muted-foreground)] transition-transform", open ? "rotate-180" : "")}
        />
      </button>

      {/* Rendered into <body>, not next to the button.
       *
       * This panel has now been broken twice by the box it used to live in,
       * each time for a reason invisible in its own markup. First `backdrop-
       * filter` on the top bar, which makes that bar the containing block for
       * fixed-position descendants AND a stacking context, so a `fixed
       * inset-0` click-away layer covered the 44px bar instead of the viewport
       * and landed on the button that had just been tapped. Then the bar's
       * `overflow-x-clip`: an absolutely-positioned panel hanging below a
       * clipped ancestor survives in Chrome, which implements
       * `overflow-x: clip` with `overflow-y: visible` as the spec says, and is
       * at the mercy of the engine anywhere that pair is handled differently —
       * a clipped y axis erases a panel that sits entirely below the bar.
       *
       * A portal has no ancestor to be clipped by, no stacking context to be
       * trapped in, and no containing block to be measured against. The
       * position is measured from the button instead, which is the one thing
       * that cannot silently change meaning.
       *
       * z-50 still matters: the sidebar drawer is z-40. */}
      {open && anchor && typeof document !== "undefined" && createPortal(
        <div
          ref={menuRef}
          style={{ position: "fixed", top: anchor.top, right: anchor.right }}
          className="z-50 max-h-[60dvh] w-64 max-w-[calc(100vw-2rem)] overflow-y-auto overscroll-contain rounded-lg border border-[var(--color-border)] bg-[var(--color-popover)] shadow-[var(--shadow-lg)]"
        >
            <div className="max-h-64 overflow-y-auto p-1">
              {list.length === 0 && (
                <div className="px-2 py-3 text-center text-xs text-[var(--color-muted-foreground)]">
                  {t("shell.noWorkspaces")}
                </div>
              )}
              {list.map((w: any) => (
                <button
                  key={w.id}
                  onClick={() => switchWs(w.slug, w.name)}
                  className={cn(
                    "flex min-h-11 w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors hover:bg-[var(--color-accent)] sm:min-h-0",
                    w.id === me?.active_id && "bg-[var(--color-brand-muted)] text-[var(--color-brand)]",
                  )}
                >
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-medium">{w.name}</div>
                    <div className="truncate text-[10px] text-[var(--color-muted-foreground)]">
                      {w.slug} · {w.role}
                    </div>
                  </div>
                  {w.id === me?.active_id && <CheckIcon className="h-3.5 w-3.5 shrink-0" />}
                </button>
              ))}
            </div>
            <button
              onClick={() => { setOpen(false); setName(""); setDialogOpen(true); }}
              className="flex w-full items-center gap-2 border-t border-[var(--color-border)] px-3 py-2 text-xs font-medium text-[var(--color-brand)] transition-colors hover:bg-[var(--color-accent)]"
            >
            <PlusIcon className="h-3.5 w-3.5" />
            {t("shell.newWorkspace")}
          </button>
        </div>,
        document.body,
      )}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("shell.newWorkspaceTitle")}</DialogTitle>
            <DialogDescription>{t("shell.newWorkspaceDesc")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="ws-name">{t("common.name")}</Label>
            <Input
              id="ws-name"
              autoFocus
              value={name}
              placeholder={t("shell.newWorkspacePlaceholder")}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && name.trim()) createWs(); }}
            />
            {name.trim() && (
              <p className="text-[11px] text-[var(--color-muted-foreground)]">
                {t("shell.slugPreview")} <code>{slugify(name)}</code>
              </p>
            )}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDialogOpen(false)}>
              {t("common.cancel")}
            </Button>
            <Button onClick={createWs} disabled={!name.trim() || creating}>
              {creating ? t("shell.creating") : t("common.create")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

const RAIL_WIDTH = 48;
const SIDEBAR_WIDTH = 240;

/** Fast out, soft landing — the curve the width and the labels share, so the
 *  column and its contents arrive together. */
const SIDEBAR_EASE = [0.32, 0.72, 0, 1] as const;

function noSubscribe(): () => void {
  return () => {};
}

/** ⌘ on a Mac, Ctrl everywhere else — for the tooltip only. The binding
 *  itself answers to both, because a Mac with an external PC keyboard is a
 *  real thing and the cost of accepting either is nothing. */
function useShortcutLabel(): string {
  const mac = useSyncExternalStore(
    noSubscribe,
    () => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent),
    () => false,
  );
  return mac ? "⌘B" : "Ctrl+B";
}

/** One sidebar row: icon always, label beside it while there is room.
 *
 *  The same element in both states, so collapsing animates rather than swaps:
 *  the column narrows, the label fades and is clipped, and the icon does not
 *  move — it sits at the rail's centre line in both widths. In the rail the
 *  label is still in the DOM (it is the link's accessible name) and a
 *  tooltip carries it for a pointer. */
function NavLink({
  section, pathname, expanded,
}: {
  section: NavSection; pathname: string; expanded: boolean;
}) {
  const t = useT();
  const active = isSectionActive(pathname, section);
  const Icon = section.icon;
  const label = t(section.labelKey);
  return (
    <FloatingTooltip label={label} disabled={expanded}>
      <Link
        href={section.href}
        data-tour={NAV_TOUR[section.href]}
        aria-current={active ? "page" : undefined}
        className={cn(
          // `shrink-0`: the nav is a column flex container, and a flex item
          // whose only height is `h-9` shrinks below it — on a short window
          // every row squeezed itself into the available space instead of
          // the nav scrolling, and the hit target went with it.
          // px-2.5 inside the nav's px-1.5 puts a 16px icon's centre at 24px,
          // the middle of the 48px rail — so it does not jump on collapse.
          "group/nav relative flex min-h-11 w-full shrink-0 items-center gap-2.5 overflow-hidden rounded-md px-2.5 text-sm transition-colors sm:h-9 sm:min-h-0",
          active
            ? "bg-[var(--color-brand-muted)] font-medium text-[var(--color-brand)]"
            : "text-[var(--color-muted-foreground)] hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]",
        )}
      >
        <Icon className="h-4 w-4 shrink-0 transition-transform duration-150 group-hover/nav:scale-110" />
        <m.span
          initial={false}
          animate={{ opacity: expanded ? 1 : 0 }}
          transition={{ duration: expanded ? 0.18 : 0.08, delay: expanded ? 0.06 : 0 }}
          className="truncate whitespace-nowrap"
        >
          {label}
        </m.span>
      </Link>
    </FloatingTooltip>
  );
}

/** The rail's footer: theme, and one round button for everything about the
 *  account — name, language, sign out. The expanded footer shows all of that
 *  at once; 48px cannot, and the rail used to offer Sign out and nothing else,
 *  so switching language meant expanding the sidebar first. The menu is a
 *  portal for the reason the workspace menu is: the column clips. */
function RailAccount() {
  const { data } = useSession();
  const t = useT();
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<{ bottom: number; left: number } | null>(null);
  const boxRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (boxRef.current?.contains(target)) return;
      if (menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = () => {
    const r = boxRef.current?.getBoundingClientRect();
    if (r) setAnchor({ bottom: window.innerHeight - r.bottom, left: r.right + 8 });
    setOpen((v) => !v);
  };
  const name = data?.user?.name || data?.user?.email || t("shell.signedIn");
  const initial = name.trim().charAt(0).toUpperCase() || "?";

  return (
    <>
      <FloatingTooltip label={t("shell.account")} disabled={open}>
        <button
          ref={boxRef}
          type="button"
          onClick={toggle}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-label={t("shell.account")}
          className="grid size-8 place-items-center rounded-full bg-[var(--color-brand-muted)] text-xs font-semibold text-[var(--color-brand)] ring-offset-2 ring-offset-[var(--color-card)] transition-shadow hover:ring-2 hover:ring-[var(--color-ring)]/40"
        >
          {initial}
        </button>
      </FloatingTooltip>
      {open && anchor && createPortal(
        // Opacity only: a transform on this box would make it the containing
        // block for the language list's fixed click-away layer.
        <m.div
          ref={menuRef}
          role="menu"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.12 }}
          style={{ position: "fixed", bottom: anchor.bottom, left: anchor.left }}
          className="z-50 w-60 rounded-lg border border-[var(--color-border)] bg-[var(--color-popover)] p-1 shadow-[var(--shadow-lg)]"
        >
          <div className="px-2.5 py-2 text-xs">
            <div className="truncate font-medium text-[var(--color-foreground)]">
              {data?.user?.name || t("shell.signedIn")}
            </div>
            <div className="truncate text-[var(--color-muted-foreground)]">{data?.user?.email}</div>
          </div>
          <div className="flex items-center justify-between gap-1 border-y border-[var(--color-border)] px-1 py-1">
            <LanguageSwitcher />
            <ThemeToggle />
          </div>
          <button
            type="button"
            role="menuitem"
            onClick={() => { forgetAgentSession(); void signOut({ callbackUrl: "/login" }); }}
            className="mt-1 flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs transition-colors hover:bg-[var(--color-accent)]"
          >
            <LogOutIcon className="h-3.5 w-3.5" />
            {t("shell.signOut")}
          </button>
        </m.div>,
        document.body,
      )}
    </>
  );
}

function matchesPage(pathname: string, page: TabDef) {
  return page.exact
    ? pathname === page.href
    : pathname === page.href || pathname.startsWith(`${page.href}/`);
}

/** A section is active when the current route belongs to any of its pages. */
function isSectionActive(pathname: string, section: NavSection) {
  return section.pages.some((p) => matchesPage(pathname, p));
}

/** Pages reachable on purpose without a sidebar entry, still named in the
 *  breadcrumb. /automation is the agent's full view: the agent itself is the
 *  round button in the corner of every page now, and an entry in the list as
 *  well would be two ways in to one thing, one of them a whole row. */
const UNLISTED_PAGES: readonly TabDef[] = [
  { href: "/automation", labelKey: "nav.automation" },
];

/** Breadcrumb «Section > Page» derived from the section map — orients the
 * user now that sub-pages live behind horizontal tabs, without duplicating
 * each page's own <h1>.
 *
 * A plain function: it walks a few dozen entries, and the React Compiler
 * memoizes the call anyway — a hand-written useMemo here was one it could not
 * preserve, and said so in the lint report. */
function breadcrumbFor(pathname: string) {
  for (const section of NAV_SECTIONS) {
    const page = section.pages.find((p) => matchesPage(pathname, p));
    if (page) {
      return {
        groupKey: page.labelKey === section.labelKey ? null : section.labelKey,
        itemKey: page.labelKey,
      };
    }
  }
  const loose = UNLISTED_PAGES.find((p) => matchesPage(pathname, p));
  return loose ? { groupKey: null, itemKey: loose.labelKey } : null;
}

export function AppShell({
  children,
  initialSidebarOpen = true,
  sidebarRemembered = true,
}: {
  children: React.ReactNode;
  /** From the sidebar cookie, read by the server — see SIDEBAR_COOKIE. */
  initialSidebarOpen?: boolean;
  /** Whether that cookie exists at all. When it does not, the pre-cookie
   *  localStorage value is migrated once — see LEGACY_SIDEBAR_KEY. */
  sidebarRemembered?: boolean;
}) {
  const pathname = usePathname();
  const { data } = useSession();
  const t = useT();
  const crumb = breadcrumbFor(pathname);
  const shortcut = useShortcutLabel();
  const reduceMotion = useReducedMotion();

  // Global-admin-only sections stay out of sight for regular members.
  const isAdmin = Boolean(data?.isAdmin);
  const navSections = useMemo(
    () => NAV_SECTIONS.filter((s) => !s.adminOnly || isAdmin),
    [isAdmin],
  );

  // Sidebar collapse — persisted so it survives navigation and reloads, and
  // known to the server so the first paint is already the right width.
  const [sidebarOpen, setSidebarOpen] = useState(initialSidebarOpen);

  // One-time migration from the localStorage-only days. Without it, everyone
  // who had collapsed the sidebar gets it expanded after the deploy. In an
  // effect because localStorage does not exist on the server; it runs only
  // for a browser with no cookie, so at most once per browser.
  useEffect(() => {
    if (sidebarRemembered) return;
    let legacy: string | null = null;
    try {
      legacy = localStorage.getItem(LEGACY_SIDEBAR_KEY);
      localStorage.removeItem(LEGACY_SIDEBAR_KEY);
    } catch { /* private mode */ }
    if (legacy !== "open" && legacy !== "closed") return;
    writeSidebarCookie(legacy);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- a one-shot migration of a value only the browser has
    if (legacy === "closed") setSidebarOpen(false);
  }, [sidebarRemembered]);

  // Below md the sidebar is an off-canvas drawer instead of a column: 240px of
  // a 390px screen left every page with a ~70px content well. Not persisted —
  // a drawer that reopens on every load is a phone anti-pattern. Closed on
  // navigation by remembering which path it was opened on, rather than by an
  // effect that sets state after every route change.
  const [mobileOpenOn, setMobileOpenOn] = useState<string | null>(null);
  const mobileOpen = mobileOpenOn === pathname;
  const setMobileOpen = useCallback(
    (open: boolean) => setMobileOpenOn(open ? pathname : null),
    [pathname],
  );
  // The drawer must never render the icon-only rail: `sidebarOpen` is the
  // desktop preference, so someone who collapsed it on desktop would
  // otherwise open a 240px-wide strip of icons on their phone.
  const expanded = sidebarOpen || mobileOpen;

  // Confirmation toast after a workspace switch (set right before the
  // WorkspaceSwitcher's full-page reload).
  useEffect(() => {
    let name: string | null = null;
    try {
      name = sessionStorage.getItem("ws-switched");
      if (name) sessionStorage.removeItem("ws-switched");
    } catch { /* private mode etc. */ }
    if (name) toast.success(t("shell.wsSwitched", { name }));
    // Once on mount only — `t` is stable enough for a one-shot toast.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggleSidebar = useCallback(() => {
    setSidebarOpen((v) => {
      const next = !v;
      const value = next ? "open" : "closed";
      writeSidebarCookie(value);
      return next;
    });
  }, []);

  // ⌘B / Ctrl+B — the binding editors and most dashboards already use for
  // the side panel. Below md it opens and closes the drawer instead, since
  // that is what the sidebar is there. Not inside an editable rich-text
  // region, where the same keys mean bold; a plain textarea has no bold, so
  // the shortcut still works from the composer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== "b" || !(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      const el = e.target as HTMLElement | null;
      if (el?.isContentEditable) return;
      e.preventDefault();
      if (window.matchMedia("(min-width: 768px)").matches) toggleSidebar();
      else setMobileOpenOn((at) => (at === pathname ? null : pathname));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleSidebar, pathname]);

  // First-visit product tour. Lives in the shell (not the dashboard page)
  // because every tour target — nav list, nav items, workspace badge — is
  // rendered by the shell itself, so the anchors are guaranteed to exist.
  // Gated to /dashboard so the tour opens where the final step's advice
  // ("restart from the Dashboard") holds true. The 800ms delay lets the
  // first paint and layout settle before driver.js measures the elements.
  useEffect(() => {
    if (pathname !== "/dashboard") return;
    // Every anchor the tour points at (nav list, nav items, workspace badge)
    // is inside the drawer, which is off-screen below md.
    if (!window.matchMedia("(min-width: 768px)").matches) return;
    try {
      if (localStorage.getItem(TOUR_DONE_KEY)) return;
    } catch {
      return; // no storage — would re-open on every visit, skip instead
    }
    const timer = setTimeout(() => startMainTour(t), 800);
    return () => clearTimeout(timer);
    // `t` only changes with locale; restarting the tour on a language switch
    // mid-tour would be more disruptive than showing the original language.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  const toggleLabel = sidebarOpen ? t("shell.collapseSidebar") : t("shell.expandSidebar");

  return (
    <div className="min-h-screen flex">
      <AnimatePresence>
        {mobileOpen && (
          <m.div
            key="backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="fixed inset-0 z-30 bg-black/60 backdrop-blur-[2px] md:hidden"
            onClick={() => setMobileOpen(false)}
            aria-hidden
          />
        )}
      </AnimatePresence>
      {/* Sign out sat at the bottom of a column that was as tall as the PAGE,
          because `md:static` let the aside stretch to the flex row's height.
          On a long page — the repository list, a review diff — the account
          block and Sign out were a full page-scroll below the fold, and
          scrolling back up to the nav was the price of leaving the app. The
          reported version of this: "sign out знаходяться внизу sidebar та до
          них треба листати багато якщо сторінка довга".

          A fixed 100dvh box, sticky at the top of the flex row, is the fix:
          the column is exactly one viewport tall no matter how long the page
          is, so `flex-1` on the nav finally means something and the footer
          below it is pinned. Sticky rather than fixed keeps the aside a flex
          item, so it still reserves its own 240px/48px of width and `main`
          needs no compensating margin — and the page keeps scrolling on the
          document, which every page below still assumes.

          Below md nothing about the drawer's geometry changes: it was already
          fixed and viewport-tall, which is why the footer was reachable there
          all along.

          The desktop width is a CSS variable that Motion animates, not an
          inline `width`: the same element is a 240px drawer on a phone, and an
          inline width would override that class at every size. `initial` is
          off, so the server-rendered width is the one that paints first. */}
      <m.aside
        id="app-sidebar"
        aria-label={t("shell.navigation")}
        initial={false}
        animate={{ "--sb-w": `${expanded ? SIDEBAR_WIDTH : RAIL_WIDTH}px` } as never}
        style={{ "--sb-w": `${expanded ? SIDEBAR_WIDTH : RAIL_WIDTH}px` } as React.CSSProperties}
        transition={{ duration: reduceMotion ? 0 : 0.26, ease: SIDEBAR_EASE }}
        className={cn(
          "fixed inset-y-0 left-0 z-40 w-60 shrink-0 overflow-hidden border-r border-[var(--color-border)] bg-[var(--color-card)] flex flex-col pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] transition-transform duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:transition-none",
          // Above md it is a plain in-flow column again, still one viewport
          // tall, whose width follows --sb-w. `bottom-auto` retires the
          // drawer's `inset-y-0` bottom edge: top + bottom + an explicit
          // height is three constraints for two, and which one a sticky box
          // drops is not worth finding out per browser.
          "md:sticky md:top-0 md:bottom-auto md:h-[100dvh] md:w-[var(--sb-w)] md:z-auto md:visible md:translate-x-0 md:pt-0 md:pb-0",
          // `invisible` as well as the transform: an off-screen drawer that is
          // still focusable means the first Tab on every page lands in a menu
          // nobody can see.
          mobileOpen ? "translate-x-0 shadow-[var(--shadow-lg)]" : "-translate-x-full invisible",
        )}
      >
        <Link
          href="/dashboard"
          aria-label="Celmis"
          className="flex h-14 shrink-0 items-center gap-2 overflow-hidden px-2.5"
        >
          {/* Same 28px mark at the same x in both widths: px-2.5 centres it
              on the rail, and the word fades in beside it. */}
          <BrandMark size="sm" className="shrink-0" />
          <m.span
            initial={false}
            animate={{ opacity: expanded ? 1 : 0 }}
            transition={{ duration: 0.16, delay: expanded ? 0.06 : 0 }}
          >
            <BrandWord className="text-base" />
          </m.span>
        </Link>

        {/* The only part that scrolls. `min-h-0` because a flex item's
            automatic minimum size is its content, which would push the
            footer off the bottom instead of scrolling; `overscroll-contain`
            so reaching the end of the nav does not start scrolling the
            page behind it. The rail hides the scrollbar: a 10px bar in a
            48px column leaves the icons off-centre. */}
        <nav
          className={cn(
            "min-h-0 flex-1 overflow-y-auto overscroll-contain px-1.5 py-2",
            !expanded && "[scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
          )}
        >
          <ul className="flex flex-col gap-0.5" data-tour="nav">
            {navSections.map((section) => (
              <li key={section.href}>
                <NavLink section={section} pathname={pathname} expanded={expanded} />
              </li>
            ))}
          </ul>
        </nav>

        {/* Pinned footer. `shrink-0` on the whole block, not on its two
            halves: flex items shrink before a sibling scrolls, so on a
            short viewport the language row and the account block would be
            squeezed instead of the nav being scrolled. The top border is
            new too — with the nav scrolling underneath it, the boundary
            has to be drawn or a half-clipped nav row reads as part of the
            footer. */}
        <div className="shrink-0 border-t border-[var(--color-border)]">
          {expanded ? (
            <m.div
              key="full"
              initial={reduceMotion ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.18, delay: 0.08 }}
              className="w-60"
            >
              <div className="flex items-center justify-between gap-1 px-2 py-2">
                <LanguageSwitcher />
                <ThemeToggle />
              </div>
              <div className="border-t border-[var(--color-border)] p-3">
                <div className="px-2 pb-2 text-xs text-[var(--color-muted-foreground)]">
                  <div className="truncate font-medium text-[var(--color-foreground)]">
                    {data?.user?.name || t("shell.signedIn")}
                  </div>
                  <div className="truncate">{data?.user?.email}</div>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  className="min-h-11 w-full justify-start sm:min-h-0"
                  onClick={() => { forgetAgentSession(); void signOut({ callbackUrl: "/login" }); }}
                >
                  <LogOutIcon className="h-4 w-4" />
                  {t("shell.signOut")}
                </Button>
              </div>
            </m.div>
          ) : (
            /* The rail's footer — theme and the account menu, icon-only. */
            <m.div
              key="rail"
              initial={reduceMotion ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.14 }}
              className="flex flex-col items-center gap-1.5 py-2"
            >
              <FloatingTooltip label={t("shell.theme")}>
                <ThemeToggle compact />
              </FloatingTooltip>
              <RailAccount />
            </m.div>
          )}
        </div>
      </m.aside>

      <main className="flex-1 flex flex-col min-w-0">
        {/* The inset has to be part of the bar's HEIGHT, not just its padding:
            a flat h-11 leaves 44px minus the inset for the row itself, i.e.
            nothing at all on a notched iPhone, so the breadcrumb and the
            switcher were squeezed into a zero-height box, half of them behind
            the status bar and the other half cut off by the clip below. The
            44px row is unchanged and env() is 0 everywhere else, so this is
            still exactly h-11 on desktop.
            Clipping only the x axis: `overflow-hidden` also cuts off anything
            hanging *below* the bar, which is where the workspace menu opens. */}
        <div className="sticky top-0 z-10 flex h-[calc(2.75rem_+_env(safe-area-inset-top))] shrink-0 items-center gap-2 overflow-x-clip border-b border-[var(--color-border)] bg-[var(--color-background)]/85 px-4 pt-[env(safe-area-inset-top)] backdrop-blur sm:gap-3">
          {/* Two buttons rather than one with a matchMedia branch: class-gated
              visibility stays correct through SSR and hydration. */}
          <button
            type="button"
            onClick={() => setMobileOpen(true)}
            aria-label={t("shell.expandSidebar")}
            aria-controls="app-sidebar"
            aria-expanded={mobileOpen}
            className="-ml-1.5 grid size-11 shrink-0 place-items-center rounded-md text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)] md:hidden"
          >
            <PanelLeftIcon className="h-5 w-5" />
          </button>
          <span className="hidden md:inline-flex">
            <FloatingTooltip label={toggleLabel} shortcut={shortcut} side="bottom">
              <button
                type="button"
                onClick={toggleSidebar}
                aria-label={toggleLabel}
                aria-controls="app-sidebar"
                aria-expanded={sidebarOpen}
                aria-keyshortcuts="Meta+B Control+B"
                className="shrink-0 rounded-md p-1.5 text-[var(--color-muted-foreground)] transition-colors hover:bg-[var(--color-accent)] hover:text-[var(--color-foreground)]"
              >
                <PanelLeftIcon className="h-4 w-4" />
              </button>
            </FloatingTooltip>
          </span>
          {crumb && (
            <nav className="flex min-w-0 items-center gap-1.5 text-xs" aria-label="Breadcrumb">
              {crumb.groupKey && (
                // The section name is the least useful half of the crumb on a
                // narrow bar — drop it before anything else is squeezed.
                <span className="hidden items-center gap-1.5 sm:flex">
                  <span className="text-[var(--color-muted-foreground)]">{t(crumb.groupKey)}</span>
                  <ChevronRightIcon className="h-3 w-3 text-[var(--color-muted-foreground)]" />
                </span>
              )}
              <span className="truncate font-medium text-[var(--color-foreground)]">{t(crumb.itemKey)}</span>
            </nav>
          )}
          <WorkspaceSwitcher />
        </div>
        {children}
        <LicenseFooter />
      </main>

      {/* Outside <main> on purpose: the top bar's backdrop-filter makes it the
          containing block for fixed descendants, and a floating button inside
          it would be positioned against a 44px strip. A sibling of <main> is
          positioned against the viewport. */}
      <AgentWidget />
    </div>
  );
}
