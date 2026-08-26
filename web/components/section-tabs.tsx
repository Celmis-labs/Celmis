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
import { useSession } from "next-auth/react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

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
  review: [
    { href: "/reviews", labelKey: "nav.reviews" },
    { href: "/admin/review-policies", labelKey: "nav.reviewPolicies" },
    { href: "/admin/agents", labelKey: "nav.agents" },
    { href: "/admin/compliance", labelKey: "nav.compliance" },
    { href: "/admin/deprecations", labelKey: "nav.deprecations" },
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
    { href: "/connections", labelKey: "nav.connections" },
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
  // /admin/{agents,compliance,deprecations,review-policies} are Code review,
  // /admin/intel is Sources, /admin/{audit,jobs,logs,notifications} are
  // Monitoring, and /admin/usage is its own section above. The rest are these.
  admin: [
    { href: "/admin/health", labelKey: "nav.health" },
    { href: "/admin/gdpr", labelKey: "nav.gdpr" },
    { href: "/admin/oauth-clients", labelKey: "nav.oauthClients" },
  ],
} as const satisfies Record<string, readonly TabDef[]>;

export type SectionKey = keyof typeof SECTION_TABS;

export type SectionTab = { href: string; label: string; exact?: boolean };

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
  // The section this route belongs to wins over the one the page asked for;
  // see sectionOwning(). Explicit `items` are never second-guessed.
  const key = items ? undefined : sectionOwning(pathname) ?? set;
  const tabs: SectionTab[] =
    items ??
    (key
      ? TAB_SETS[key]
          .filter((d: TabDef) => !d.adminOnly || isAdmin)
          .map((d: TabDef) => ({
            href: d.href,
            label: t(d.labelKey),
            exact: d.exact,
          }))
      : []);
  if (tabs.length === 0) return null;

  return (
    <nav
      aria-label="Section"
      className={cn(
        "flex gap-1 overflow-x-auto border-b border-[var(--color-border)]",
        className,
      )}
    >
      {tabs.map((tab) => {
        const active = matchesTab(pathname, tab);
        return (
          <Link
            key={tab.href}
            href={tab.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              // These are the section navigation — 38px on a phone made
              // switching between Reviews / Policies / Agents a coin flip.
              "-mb-px inline-flex min-h-11 items-center whitespace-nowrap border-b-2 px-3 py-2 text-sm transition-colors sm:min-h-0",
              active
                ? "border-[var(--color-brand)] font-medium text-[var(--color-foreground)]"
                : "border-transparent text-[var(--color-muted-foreground)] hover:border-[var(--color-border)] hover:text-[var(--color-foreground)]",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
