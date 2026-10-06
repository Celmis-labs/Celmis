"use client";

/**
 * Horizontal section tabs — the second navigation level.
 *
 * The sidebar holds one entry per top-level section; every sub-page of a
 * section is reachable through this tab row rendered under the page's H1. Tabs
 * are plain links to existing routes (no URL moves, no redirects), so deep
 * links and bookmarks keep working.
 *
 * `SECTION_TABS` is the single source of truth for which routes belong to
 * which section — the sidebar (app-shell) derives its active-state and
 * breadcrumb from these same lists, and a page's `set` prop is checked against
 * them rather than trusted (see sectionOwning).
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronDownIcon } from "lucide-react";
import { useSession } from "next-auth/react";
import { useT } from "@/lib/i18n";
import { useCanEditPrompts, useCanViewAnalytics } from "@/lib/use-analytics-access";
import { useCanManageWorkspace } from "@/lib/use-workspace-role";
import { featureOff, useCapabilities, useFeatureOff } from "@/lib/use-capabilities";
import { cn } from "@/lib/utils";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

export type TabDef = {
  href: string;
  labelKey: string;
  /** Match the path exactly — for parents like /settings whose sub-routes
   * (/settings/llm) are separate tabs. */
  exact?: boolean;
  /** Hidden from non-platform-admins. The log tail is the case: its buffer
   * holds every workspace's lines, so it is a platform view living inside a
   * section that is otherwise workspace-scoped. */
  adminOnly?: boolean;
  /** Hidden from everyone but the superadmin (the env master account):
   * /admin/users hands out owner/admin/editor, which nobody else may. */
  superadminOnly?: boolean;
  /** Hidden unless the person may read review analytics — global admin, or
   * owner / admin / editor of the active workspace (useCanViewAnalytics) —
   * and also when /api/capabilities explicitly reports `review_analytics`
   * off: an enterprise feature this installation is not licensed for. */
  analyticsOnly?: boolean;
  /** Hidden unless the person is owner / admin / editor of the active
   * workspace (or a global admin) — `require_memories_access`: viewers and
   * members get a 403 from /api/memories, so no tab leads there. */
  editorOnly?: boolean;
  /** Hidden when /api/capabilities explicitly reports this feature off — an
   *  enterprise page whose routes this installation does not mount. */
  requiresFeature?: string;
  /** Owner or admin of the active workspace (or a global admin): the same
   *  audience as spend and review cost. */
  workspaceAdminOnly?: boolean;
  /** Listed under a "More" menu at the end of the row instead of as a tab
   *  of its own: still part of the section (sidebar highlight, breadcrumb),
   *  just not worth a permanent slot. */
  more?: boolean;
};

export const SECTION_TABS = {
  // The Dashboard section had these three routes mapped for its breadcrumb but
  // rendered no tab row, so the setup wizard and the playbook were reachable
  // only from a dashboard banner that hides itself once a repo exists. Two
  // finished pages nobody could find.
  dashboard: [
    { href: "/dashboard", labelKey: "nav.dashboard", exact: true },
    { href: "/onboarding", labelKey: "nav.onboarding" },
    { href: "/capabilities", labelKey: "capabilities.title" },
  ],
  sources: [
    { href: "/repositories", labelKey: "nav.repositories" },
    { href: "/dependencies", labelKey: "nav.dependencies" },
    { href: "/docs", labelKey: "docs.title" },
    { href: "/admin/intel", labelKey: "nav.intel" },
  ],
  // Code review: the work (reviews, the PRs they ran on, the findings they
  // left), what steers it (rules, settings) and the lead's view. Settings is
  // ONE tab: the workspace defaults, every repository's overrides and the
  // agent prompts used to be three tabs (Review defaults, Review policies,
  // AI Agents) holding thirds of the same settings; their routes redirect to
  // /review-settings. Compliance and Deprecations are rarely opened and sit
  // under "More" so the row stays one line at 1024px.
  review: [
    { href: "/reviews", labelKey: "nav.reviews" },
    { href: "/pull-requests", labelKey: "prs.navLabel" },
    // Findings followed across a PR's pushes.
    { href: "/issues", labelKey: "issues.navLabel" },
    // The rules library: workspace and per-repository rules, the built-in
    // library, generated and imported proposals waiting for approval.
    { href: "/admin/review-rules", labelKey: "nav.reviewRules" },
    // What the team has taught the reviewers: facts, per repository or
    // directory, told to every review; the pending ones wait for approval.
    { href: "/memories", labelKey: "nav.memories", editorOnly: true },
    { href: "/review-settings", labelKey: "nav.reviewSettings" },
    { href: "/analytics", labelKey: "analytics.navLabel", analyticsOnly: true },
    // Same licence as Analytics but a narrower audience (owner and admin); its
    // own capability, so a build that mounts one and not the other shows only
    // the tab that works.
    { href: "/productivity", labelKey: "productivity.navLabel", workspaceAdminOnly: true, requiresFeature: "productivity" },
    { href: "/admin/compliance", labelKey: "nav.compliance", more: true },
    { href: "/admin/deprecations", labelKey: "nav.deprecations", more: true },
  ],
  qa: [
    { href: "/projects", labelKey: "nav.projects" },
    { href: "/chats", labelKey: "nav.chats" },
    { href: "/search", labelKey: "nav.search" },
  ],
  agent: [
    { href: "/claude", labelKey: "nav.sessions" },
  ],
  // Alerts came in under "Agent" and the channels that deliver them sat in
  // "Settings", two clicks and one mental leap apart, while the server log
  // tail lived in the platform-admin section. Three halves of one job.
  monitoring: [
    { href: "/alerts", labelKey: "nav.alerts" },
    { href: "/admin/notifications", labelKey: "nav.notifications" },
    // The queue moved here from the admin section the moment its rows learned
    // which workspace they belong to. It answers "is my indexing running",
    // which is a monitoring question and never was an administration one —
    // it lived under a global-admin flag only because the endpoint could not
    // tell one tenant's jobs from another's.
    { href: "/admin/jobs", labelKey: "nav.jobs" },
    // The audit trail followed it for the same reason, one turn of the same
    // screw: an `AuditRecord` had no workspace on it, so the log could only
    // be all-or-nothing and was parked in Administration behind a global-admin
    // flag. Now the record carries its tenant and /api/audit scopes the read,
    // and this is the section where it belongs — «what did my workspace do,
    // and did any of it fail» is the same question Alerts and the Job queue
    // answer, one level down: per LLM call rather than per job. It is not a
    // billing page (there is no cost on it, that is Usage & cost) and there is
    // nothing on it to administer — it is read-only history, sitting beside
    // the server log tail it is the workspace-scoped counterpart of.
    { href: "/admin/audit", labelKey: "nav.audit" },
    { href: "/admin/logs", labelKey: "nav.logs", adminOnly: true },
  ],
  settings: [
    { href: "/settings", labelKey: "nav.account", exact: true },
    { href: "/settings/llm", labelKey: "nav.llm" },
    { href: "/settings/models", labelKey: "nav.models" },
    { href: "/connections", labelKey: "nav.connections", workspaceAdminOnly: true },
    { href: "/settings/mcp", labelKey: "nav.mcp" },
  ],
  team: [
    { href: "/admin/workspaces", labelKey: "nav.workspaces" },
    { href: "/admin/teams", labelKey: "nav.teams" },
    { href: "/admin/access", labelKey: "nav.access" },
  ],
  // Usage & cost is its own section, not a page of Administration. It used to
  // be listed under `admin`, which is where the breadcrumb and the tab row
  // read their answers from, so opening a workspace's own bill said
  // «Administration > Usage & cost» and offered Job queue / System status /
  // Audit log as its neighbours — global-infrastructure pages that the
  // workspace owner reading the bill has no business in and, not being a
  // global admin, cannot open. Asked about it directly: "чому job у cost and
  // usage а не у administration". Because the section owned the page.
  //
  // Its own key, so nothing is borrowed: the sidebar highlights Usage while
  // you are on it, the breadcrumb says «Usage & cost», and the tab row is one
  // tab wide because the section is one page deep.
  usage: [
    { href: "/admin/usage", labelKey: "nav.usage" },
  ],
  // What is left is the global-infrastructure section proper — every /admin
  // route that no workspace-scoped section claims. Sanity check for anyone
  // adding a page here: /admin/{access,teams,workspaces} are Team,
  // /admin/{compliance,deprecations,review-rules} are Code review (the old
  // /admin/{agents,review-defaults,review-policies} redirect to /review-settings),
  // /admin/intel is Sources, /admin/{audit,jobs,logs,notifications} are
  // Monitoring, and /admin/usage is its own section above. The rest are these.
  admin: [
    { href: "/admin/health", labelKey: "nav.health" },
    { href: "/admin/gdpr", labelKey: "nav.gdpr" },
    { href: "/admin/oauth-clients", labelKey: "nav.oauthClients" },
    { href: "/admin/users", labelKey: "nav.users", superadminOnly: true },
    { href: "/admin/access-requests", labelKey: "nav.accessRequests", superadminOnly: true },
    { href: "/admin/mcp-tokens", labelKey: "nav.mcpTokens", superadminOnly: true },
  ],
} as const satisfies Record<string, readonly TabDef[]>;

export type SectionKey = keyof typeof SECTION_TABS;

export type SectionTab = { href: string; label: string; exact?: boolean; more?: boolean };

/** Widened view of the same object. Indexing SECTION_TABS with a `SectionKey`
 * variable yields a union of readonly tuples, and calling `.some()` on such a
 * union is a type error even though every member has the method. */
const TAB_SETS: Record<SectionKey, readonly TabDef[]> = SECTION_TABS;

function matchesTab(pathname: string, tab: { href: string; exact?: boolean }) {
  return tab.exact
    ? pathname === tab.href
    : pathname === tab.href || pathname.startsWith(`${tab.href}/`);
}

/** Which section this route actually belongs to.
 *
 * A page names its own tab set, and a page can be wrong about it — /admin/usage
 * asks for `set="admin"`, which was true while Usage lived in Administration
 * and is not any more. A tab row in which nothing is active is worse than no
 * row at all: it says you are somewhere you are not. So the route decides
 * which row to draw, and the prop is the fallback for routes no section
 * claims. Every set stays a pure function of SECTION_TABS either way, which is
 * what keeps a tab and the sidebar entry above it from disagreeing. */
function sectionOwning(pathname: string): SectionKey | undefined {
  return (Object.keys(TAB_SETS) as SectionKey[]).find((key) =>
    TAB_SETS[key].some((tab) => matchesTab(pathname, tab)),
  );
}

export function SectionTabs({
  items,
  set,
  className,
}: {
  /** Explicit tabs (already-translated labels) … */
  items?: SectionTab[];
  /** … or a predefined set from SECTION_TABS. */
  set?: SectionKey;
  className?: string;
}) {
  const pathname = usePathname();
  const t = useT();
  // Same source the sidebar filters on, so a tab and its section can never
  // disagree about who may see it.
  const { data: session } = useSession();
  const isAdmin = Boolean(session?.isAdmin);
  const isSuperadmin = Boolean(session?.isSuperadmin);
  // `undefined` while loading counts as no: a tab that appears late is
  // better than one that appears and is then taken away.
  // Only an explicit `false` from the server hides it (the capabilities
  // contract): no answer keeps today's behaviour.
  const analyticsOff = useFeatureOff("review_analytics");
  const canAnalytics = useCanViewAnalytics() === true && !analyticsOff;
  const canEditor = useCanEditPrompts() === true;
  const canManage = useCanManageWorkspace() === true;
  const capabilities = useCapabilities().data;
  // The section this route belongs to wins over the one the page asked for;
  // see sectionOwning(). Explicit `items` are never second-guessed.
  const key = items ? undefined : sectionOwning(pathname) ?? set;
  const tabs: SectionTab[] =
    items ??
    (key
      ? TAB_SETS[key]
          .filter((d: TabDef) => !d.adminOnly || isAdmin)
          .filter((d: TabDef) => !d.superadminOnly || isSuperadmin)
          .filter((d: TabDef) => !d.analyticsOnly || canAnalytics)
          .filter((d: TabDef) => !d.editorOnly || canEditor)
          .filter((d: TabDef) => !d.workspaceAdminOnly || canManage)
          .filter((d: TabDef) => !d.requiresFeature || !featureOff(capabilities, d.requiresFeature))
          .map((d: TabDef) => ({
            href: d.href,
            label: t(d.labelKey),
            exact: d.exact,
            more: d.more,
          }))
      : []);
  if (tabs.length === 0) return null;
  const primary = tabs.filter((tab) => !tab.more);
  const more = tabs.filter((tab) => tab.more);
  const activeMore = more.find((tab) => matchesTab(pathname, tab));

  const tabClass = (active: boolean) => cn(
    // These are the section navigation — 38px on a phone made
    // switching between Reviews / Rules / Settings a coin flip.
    "-mb-px inline-flex min-h-11 items-center gap-1 whitespace-nowrap border-b-2 px-3 py-2 text-sm transition-colors sm:min-h-0",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-ring)]",
    active
      ? "border-[var(--color-brand)] font-medium text-[var(--color-foreground)]"
      : "border-transparent text-[var(--color-muted-foreground)] hover:border-[var(--color-border)] hover:text-[var(--color-foreground)]",
  );

  return (
    <nav
      aria-label={t("nav.sectionTabs")}
      className={cn(
        "flex gap-1 overflow-x-auto border-b border-[var(--color-border)]",
        className,
      )}
    >
      {primary.map((tab) => {
        const active = matchesTab(pathname, tab);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            aria-current={active ? "page" : undefined}
            className={tabClass(active)}
          >
            {tab.label}
          </Link>
        );
      })}
      {more.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-current={activeMore ? "page" : undefined}
              className={tabClass(Boolean(activeMore))}
            >
              {activeMore ? activeMore.label : t("nav.more")}
              <ChevronDownIcon className="h-3.5 w-3.5 opacity-60" aria-hidden />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            {more.map((tab) => (
              <DropdownMenuItem key={tab.href} asChild>
                <Link
                  href={tab.href}
                  aria-current={matchesTab(pathname, tab) ? "page" : undefined}
                  className="cursor-pointer"
                >
                  {tab.label}
                </Link>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </nav>
  );
}
