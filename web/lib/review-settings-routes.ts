/**
 * Where the code review settings live, and where the old pages send people.
 *
 * One route, /review-settings, edits both layers of every review setting:
 * the workspace defaults ("Global") and each repository's overrides. The
 * scope and the section are query parameters — a repository slug can carry a
 * slash, which a path segment would split — so a link can open exactly the
 * card it is about:
 *
 *   /review-settings                               Global › General
 *   /review-settings?section=categories            Global › Review categories
 *   /review-settings?repo=acme-api&section=prompts&agent=security
 *
 * The three pages this replaced (/admin/review-defaults, /admin/review-policies
 * and its per-repo page, /admin/agents and its per-agent page) are redirects
 * built from the maps below, so a bookmark or an old answer from the agent
 * still lands on the card it meant.
 *
 * Plain module, no "use client": the redirect pages are server components.
 */

export const SETTINGS_ROUTE = "/review-settings";

export const SECTION_IDS = [
  "general",
  "categories",
  "filters",
  "prompts",
  "summary",
  "rules",
  "messages",
  "commands",
  "learning",
  "advanced",
] as const;
export type SectionId = (typeof SECTION_IDS)[number];

/** Sections that exist only for a repository. The Global scope has no MCP
 *  sources and no legacy folder rules — both are per-repository rows. */
export const REPO_ONLY_SECTIONS: readonly SectionId[] = ["advanced"];

export function isSectionId(value: unknown): value is SectionId {
  return typeof value === "string" && (SECTION_IDS as readonly string[]).includes(value);
}

/** The section a URL names, or the first one; a repo-only section asked for
 *  at Global falls back to General rather than to an empty page. */
export function sectionFromParam(value: unknown, isRepo: boolean): SectionId {
  if (!isSectionId(value)) return "general";
  if (!isRepo && REPO_ONLY_SECTIONS.includes(value)) return "general";
  return value;
}

export function settingsHref(opts: {
  repo?: string | null;
  section?: SectionId | null;
  agent?: string | null;
} = {}): string {
  const qs = new URLSearchParams();
  if (opts.repo) qs.set("repo", opts.repo);
  if (opts.section && opts.section !== "general") qs.set("section", opts.section);
  if (opts.agent) qs.set("agent", opts.agent);
  const s = qs.toString();
  return s ? `${SETTINGS_ROUTE}?${s}` : SETTINGS_ROUTE;
}

/** /admin/review-defaults?tab=… → the section that now holds that tab's
 *  first card. "comments" held the inline-comment threshold first. */
export const LEGACY_DEFAULTS_TAB: Record<string, SectionId> = {
  agents: "categories",
  comments: "filters",
  ignore: "filters",
};

/** /admin/review-policies/<repo>?tab=… → its new section. "agents" held the
 *  participation switches first, "models" the per-agent model rows — both
 *  now in Review categories. */
export const LEGACY_POLICY_TAB: Record<string, SectionId> = {
  general: "general",
  agents: "categories",
  rules: "rules",
  comments: "filters",
  ignore: "filters",
  models: "categories",
  mcp: "advanced",
};

function one(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export type LegacySearchParams = Record<string, string | string[] | undefined>;

export function legacyDefaultsHref(params: LegacySearchParams): string {
  return settingsHref({ section: LEGACY_DEFAULTS_TAB[one(params.tab) ?? ""] ?? null });
}

export function legacyPolicyHref(slug: string | null, params: LegacySearchParams): string {
  return settingsHref({
    repo: slug,
    section: LEGACY_POLICY_TAB[one(params.tab) ?? ""] ?? null,
  });
}

export function legacyAgentHref(name: string | null): string {
  return settingsHref({ section: "prompts", agent: name });
}
