"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  SettingsIcon,
  ShieldCheckIcon,
  ShieldOffIcon,
} from "lucide-react";

import {
  api,
  reviewPoliciesApi,
  type RepoOut,
  type ReviewPolicyListItem,
} from "@/lib/api";
import { useToken } from "@/lib/use-token";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

/**
 * Above this many repos a dropdown turns into a scroll-hunt, so the repo
 * picker degrades to a text input + <datalist> (type-ahead over the very
 * same options) instead of a <Select>.
 */
const REPO_SELECT_MAX = 15;

/**
 * One rendered line. `policy === null` means the repo has no row in
 * `repo_review_policies` yet — it is NOT excluded from review, it runs on the
 * backend defaults (GET /{slug} synthesizes them, PUT creates the row). Such
 * rows are rendered anyway: otherwise a freshly added repo would be
 * unreachable — invisible in the list, and the only page that could create its
 * policy is the one the list links to.
 */
type PolicyRowData = { slug: string; policy: ReviewPolicyListItem | null };

export default function ReviewPoliciesIndexPage() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const [repoFilter, setRepoFilter] = useState("");
  const [department, setDepartment] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  // Fetched UNFILTERED on purpose: this single response feeds both the rendered
  // rows and the two filter option lists. Filtering server-side would shrink the
  // options to whatever survived the current filter — a dead end (pick a repo,
  // and it becomes the only repo you can still pick).
  const policies = useQuery({
    queryKey: ["review-policies", "list"],
    queryFn: () => reviewPoliciesApi.list(token!),
    enabled: !!token,
  });

  // The repo inventory of the active workspace. Same queryKey the rest of the
  // app uses (dashboard, repositories, reviews…), so this is normally served
  // straight from the react-query cache — no extra round-trip.
  const repos = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<RepoOut[]>("/api/repos", { token }),
    enabled: !!token,
  });

  const all = useMemo<PolicyRowData[]>(() => {
    const saved = new Map(
      (policies.data ?? []).map((p) => [p.repo_slug, p] as const),
    );
    const slugs = new Set<string>((repos.data ?? []).map((r) => r.slug));
    // A policy whose repo has since been removed from the inventory still
    // exists in the DB and would still apply if the repo came back — keep it
    // listed so it stays resettable instead of silently disappearing.
    saved.forEach((_p, slug) => slugs.add(slug));
    // Configured repos first (that is the "what did I customize" question this
    // page answers), everything else after; alphabetical inside each group.
    return Array.from(slugs, (slug) => ({ slug, policy: saved.get(slug) ?? null }))
      .sort((a, b) => {
        if (!!a.policy !== !!b.policy) return a.policy ? -1 : 1;
        return a.slug.localeCompare(b.slug);
      });
  }, [policies.data, repos.data]);

  // Every repo of the workspace, not just the configured ones.
  const repoSlugs = useMemo(
    () => all.map((r) => r.slug).sort((a, b) => a.localeCompare(b)),
    [all],
  );

  const departments = useMemo(() => {
    const set = new Set<string>();
    (policies.data ?? []).forEach((p) => p.department && set.add(p.department));
    return Array.from(set).sort();
  }, [policies.data]);

  // A department that no longer exists (its last repo lost the label) would
  // otherwise filter everything out while its own <Select> is disabled — i.e.
  // an empty list with no way back. Fall back to "all".
  const activeDepartment = departments.includes(department) ? department : "";

  const visible = useMemo(() => {
    const needle = repoFilter.trim().toLowerCase();
    // Picked from the list → exact slug. Typed by hand into the datalist input
    // → substring, so a half-typed slug still narrows the list down.
    const exact = repoSlugs.includes(repoFilter);
    return all.filter((r) => {
      // Only a saved policy can carry a department, so this filter necessarily
      // hides the defaults rows — that is the point of the filter.
      if (activeDepartment && r.policy?.department !== activeDepartment) return false;
      if (!needle) return true;
      return exact
        ? r.slug === repoFilter
        : r.slug.toLowerCase().includes(needle);
    });
  }, [all, activeDepartment, repoFilter, repoSlugs]);

  const configured = useMemo(() => visible.filter((r) => r.policy), [visible]);
  const onDefaults = useMemo(() => visible.filter((r) => !r.policy), [visible]);

  const loading = policies.isLoading || repos.isLoading;
  const loadError = (policies.error ?? repos.error) as Error | null;

  /** Flip the master switch without opening the detail page. */
  const toggleEnabled = async (slug: string, next: boolean) => {
    if (!token) return;
    try {
      // For a repo without a row the GET returns the synthetic defaults, so the
      // PUT below materializes the policy with everything else at its default.
      const current = await reviewPoliciesApi.get(token, slug);
      // PUT is a full replace — every field must be echoed back or the toggle
      // silently wipes models/prompts/MCP/agent switches.
      await reviewPoliciesApi.upsert(token, slug, {
        enabled: next,
        prompt_template: current.prompt_template,
        target_branches: current.target_branches,
        folder_rules: current.folder_rules,
        department: current.department,
        architect_model: current.architect_model ?? null,
        security_model: current.security_model ?? null,
        quality_model: current.quality_model ?? null,
        tests_model: current.tests_model ?? null,
        verifier_model: current.verifier_model ?? null,
        agent_prompt_overrides: current.agent_prompt_overrides ?? {},
        mcp_sources: current.mcp_sources ?? [],
        disabled_agents: current.disabled_agents ?? [],
      });
      qc.invalidateQueries({ queryKey: ["review-policies"] });
      toast.success(
        next
          ? t("admin.reviewPolicies.reviewEnabledToast")
          : t("admin.reviewPolicies.reviewDisabledToast"),
      );
    } catch (e) {
      toast.error(
        t("admin.reviewPolicies.toggleFailed", { message: (e as Error).message }),
      );
    }
  };

  const renderRow = (row: PolicyRowData) => (
    <PolicyRow
      key={row.slug}
      row={row}
      expanded={expanded === row.slug}
      onToggle={() => setExpanded(expanded === row.slug ? null : row.slug)}
      onToggleEnabled={(next) => toggleEnabled(row.slug, next)}
    />
  );

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<ShieldCheckIcon className="h-6 w-6" />}
        title={t("admin.reviewPolicies.title")}
        description={t("admin.reviewPolicies.description")}
        tabs={<SectionTabs set="review" />}
      />

      <Callout tone="info">
        <p>{t("admin.reviewPolicies.explainerWhat")}</p>
        <p className="mt-1">{t("admin.reviewPolicies.explainerDefaults")}</p>
      </Callout>

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.filterTitle")}</CardTitle>
          <CardDescription>{t("admin.reviewPolicies.filterDescription")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <Label htmlFor="repo">{t("admin.reviewPolicies.searchLabel")}</Label>
            {repoSlugs.length > REPO_SELECT_MAX ? (
              <>
                <Input
                  id="repo"
                  list="review-policy-repos"
                  placeholder={t("admin.reviewPolicies.searchPlaceholder")}
                  value={repoFilter}
                  onChange={(e) => setRepoFilter(e.target.value)}
                />
                <datalist id="review-policy-repos">
                  {repoSlugs.map((s) => (
                    <option key={s} value={s} />
                  ))}
                </datalist>
              </>
            ) : (
              <Select
                id="repo"
                className="w-full"
                value={repoFilter}
                onChange={(v) => setRepoFilter(v)}
                options={[
                  { value: "", label: t("admin.reviewPolicies.allReposOption") },
                  ...repoSlugs.map((s) => ({ value: s, label: s })),
                ]}
              />
            )}
            {/* Filters this list only, client-side, over every repo of the
                workspace — configured or running on defaults. */}
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.searchHint")}
            </p>
          </div>
          <div className="space-y-1">
            <Label htmlFor="dept">{t("admin.reviewPolicies.departmentLabel")}</Label>
            <Select
              id="dept"
              className="w-full"
              value={activeDepartment}
              onChange={(v) => setDepartment(v)}
              disabled={departments.length === 0}
              options={[
                { value: "", label: t("admin.reviewPolicies.allOption") },
                ...departments.map((d) => ({ value: d, label: d })),
              ]}
            />
            {/* `department` is a free-text column read only by this filter —
                nothing in src/review/* branches on it. */}
            {departments.length === 0 && (
              <p className="text-xs text-[var(--color-muted-foreground)]">
                {t("admin.reviewPolicies.departmentsEmptyHint")}
              </p>
            )}
            <p className="text-xs text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.departmentHint")}
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("admin.reviewPolicies.configuredReposTitle")}</CardTitle>
          <CardDescription>
            {t("admin.reviewPolicies.configuredReposDescription")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {loading && <p className="text-sm">{t("admin.reviewPolicies.loading")}</p>}

          {!loading && loadError && (
            <Callout tone="danger">
              {t("common.loadError")}: {loadError.message}
            </Callout>
          )}

          {!loading && !loadError && all.length === 0 && (
            <EmptyState
              icon={ShieldCheckIcon}
              title={t("admin.reviewPolicies.emptyState")}
              description={t("admin.reviewPolicies.emptyStateHint")}
              action={
                <Link href="/repositories">
                  <Button variant="outline">
                    {t("admin.reviewPolicies.emptyStateCta")}
                  </Button>
                </Link>
              }
            />
          )}

          {!loading && all.length > 0 && visible.length === 0 && (
            <p className="text-sm text-[var(--color-muted-foreground)]">
              {t("admin.reviewPolicies.noMatches")}
            </p>
          )}

          {configured.length > 0 && (
            <>
              <GroupHeading
                label={t("admin.reviewPolicies.groupConfigured", {
                  count: configured.length,
                })}
              />
              {configured.map(renderRow)}
            </>
          )}

          {onDefaults.length > 0 && (
            <>
              <GroupHeading
                label={t("admin.reviewPolicies.groupDefaults", {
                  count: onDefaults.length,
                })}
              />
              {onDefaults.map(renderRow)}
            </>
          )}
        </CardContent>
      </Card>
    </PageShell>
  );
}

function GroupHeading({ label }: { label: string }) {
  return (
    <div className="pt-2 text-[11px] font-medium uppercase tracking-wide text-[var(--color-muted-foreground)]">
      {label}
    </div>
  );
}

function PolicyRow({
  row,
  expanded,
  onToggle,
  onToggleEnabled,
}: {
  row: PolicyRowData;
  expanded: boolean;
  onToggle: () => void;
  onToggleEnabled: (next: boolean) => void;
}) {
  const t = useT();
  const { slug, policy } = row;
  const href = `/admin/review-policies/${encodeURIComponent(slug)}`;
  return (
    <div className="rounded-lg border border-[var(--color-border)]">
      <div className="flex items-center gap-3 px-3 py-2">
        <button
          type="button"
          onClick={onToggle}
          className="rounded p-1 hover:bg-[var(--color-accent)]"
          title={expanded ? t("admin.reviewPolicies.collapse") : t("admin.reviewPolicies.expandBranches")}
        >
          {expanded ? (
            <ChevronDownIcon className="h-4 w-4" />
          ) : (
            <ChevronRightIcon className="h-4 w-4" />
          )}
        </button>
        <div className="flex-1 min-w-0">
          <div className="font-medium truncate flex items-center gap-2">
            <Link href={href} className="truncate hover:underline">
              {slug}
            </Link>
            {policy ? (
              <>
                {!policy.enabled && (
                  <Badge variant="destructive" className="text-[10px]">
                    <ShieldOffIcon className="h-3 w-3 mr-1" /> {t("admin.reviewPolicies.disabledBadge")}
                  </Badge>
                )}
                {policy.has_custom_prompt && (
                  <Badge variant="outline" className="text-[10px]">{t("admin.reviewPolicies.promptBadge")}</Badge>
                )}
                {policy.folder_rules_count > 0 && (
                  <Badge variant="outline" className="text-[10px]">
                    {t("admin.reviewPolicies.folderRules", { count: policy.folder_rules_count })}
                  </Badge>
                )}
                {(policy.disabled_agents?.length ?? 0) > 0 && (
                  <Badge variant="outline" className="text-[10px]">
                    {t("admin.reviewPolicies.agentsOffBadge", {
                      count: policy.disabled_agents?.length ?? 0,
                    })}
                  </Badge>
                )}
                {policy.department && (
                  <Badge variant="outline" className="text-[10px]">
                    {policy.department}
                  </Badge>
                )}
              </>
            ) : (
              <Badge variant="outline" className="text-[10px]">
                {t("admin.reviewPolicies.defaultsBadge")}
              </Badge>
            )}
          </div>
          <div className="text-xs text-[var(--color-muted-foreground)] mt-0.5">
            {policy ? (
              <>
                {policy.target_branches.length === 0
                  ? t("admin.reviewPolicies.allBranches")
                  : t("admin.reviewPolicies.branchesList", { branches: policy.target_branches.join(", ") })}
                {" · "}
                {t("admin.reviewPolicies.updatedAt", { date: new Date(policy.updated_at).toLocaleString() })}
              </>
            ) : (
              t("admin.reviewPolicies.defaultsRowHint")
            )}
          </div>
        </div>
        {/* A repo without a row IS enabled (the backend defaults say so), so the
            switch shows the truth. Flipping it PUTs the defaults with the new
            value and creates the row — nothing can be lost, there is nothing
            stored yet. The Switch is fully controlled, so a rejected PUT leaves
            it visually untouched instead of lying. */}
        <span title={policy ? undefined : t("admin.reviewPolicies.defaultsSwitchHint")}>
          <Switch
            checked={policy ? policy.enabled : true}
            onCheckedChange={onToggleEnabled}
            aria-label={t("admin.reviewPolicies.enableReviewAria")}
          />
        </span>
        <Link href={href}>
          <Button variant="ghost" size="icon" title={t("admin.reviewPolicies.settingsTitle")}>
            <SettingsIcon className="h-4 w-4" />
          </Button>
        </Link>
      </div>

      {expanded && <BranchesPanel slug={slug} />}
    </div>
  );
}

function BranchesPanel({ slug }: { slug: string }) {
  const t = useT();
  const token = useToken();
  const branches = useQuery({
    queryKey: ["review-policies", "branches", slug],
    queryFn: () => reviewPoliciesApi.branches(token!, slug),
    enabled: !!token,
  });

  // Works for repos without a policy too — GET returns the synthetic defaults,
  // i.e. an empty target list ("all branches").
  const detail = useQuery({
    queryKey: ["review-policies", "detail", slug],
    queryFn: () => reviewPoliciesApi.get(token!, slug),
    enabled: !!token,
  });

  if (branches.isLoading || detail.isLoading) {
    return <div className="px-4 pb-3 text-xs text-[var(--color-muted-foreground)]">{t("admin.reviewPolicies.loadingBranches")}</div>;
  }

  const targetSet = new Set(detail.data?.target_branches ?? []);

  return (
    <div className="px-4 pb-3 pt-1 border-t border-[var(--color-border)]">
      <p className="text-xs text-[var(--color-muted-foreground)] mb-2">
        {t("admin.reviewPolicies.branchesHint")}
      </p>
      <div className="flex flex-wrap gap-2">
        {(branches.data?.branches ?? []).map((b) => (
          <span
            key={b}
            className={`text-xs rounded border px-2 py-1 ${
              targetSet.has(b)
                ? "bg-[var(--color-primary)] text-[var(--color-primary-foreground)] border-transparent"
                : "border-[var(--color-border)] text-[var(--color-muted-foreground)]"
            }`}
          >
            {b}
            {branches.data?.default_branch === b && " ★"}
          </span>
        ))}
        {(branches.data?.branches?.length ?? 0) === 0 && (
          <span className="text-xs text-[var(--color-muted-foreground)]">
            {t("admin.reviewPolicies.repoNotCloned")}
          </span>
        )}
      </div>
    </div>
  );
}
