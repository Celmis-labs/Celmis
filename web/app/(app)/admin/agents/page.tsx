"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  BotIcon, SettingsIcon, CheckCircle2Icon, FileEditIcon, GitForkIcon,
} from "lucide-react";

import { agentsApi, reviewPoliciesApi } from "@/lib/api";
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

/** Where a repository's per-agent prompt is edited. */
function repoPromptHref(slug: string): string {
  return `/admin/review-policies/${encodeURIComponent(slug)}?tab=agents`;
}

export default function AgentsIndexPage() {
  const t = useT();
  const token = useToken();
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: () => agentsApi.list(token!),
    enabled: !!token,
  });
  // Which repositories override each agent's prompt — a workspace prompt
  // edited here never reaches them, and this page is where people look.
  const overrides = useQuery({
    queryKey: ["review-policies", "overrides-summary"],
    queryFn: () => reviewPoliciesApi.overridesSummary(token!),
    enabled: !!token,
  });

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<BotIcon className="h-6 w-6" />}
        title={t("admin.agents.title")}
        description={
          <>
            {t("admin.agents.descriptionLead")}{" "}
            <Link className="underline" href="/admin/review-policies">
              {t("admin.agents.reviewPoliciesLink")}
            </Link>
            .
          </>
        }
        tabs={<SectionTabs set="review" />}
      />

      <Callout tone="info">
        <span className="font-medium">{t("admin.agents.precedenceTitle")}</span>{" "}
        {t("admin.agents.precedenceBody")}
      </Callout>

      {agents.isLoading && <p className="text-sm">{t("admin.agents.loading")}</p>}

      <div className="grid gap-3">
        {agents.data?.map((a) => (
          <Card key={a.name}>
            <CardHeader>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <CardTitle className="flex items-center gap-2">
                    {a.display_name}
                    {a.has_override && (
                      <Badge variant="outline" className="text-[10px]">
                        <FileEditIcon className="h-3 w-3 mr-1" /> {t("admin.agents.customPrompt")}
                      </Badge>
                    )}
                    <Badge
                      variant={
                        a.verdict_impact.startsWith("critical")
                          ? "destructive"
                          : "outline"
                      }
                      className="text-[10px]"
                    >
                      {a.verdict_impact.startsWith("critical")
                        ? t("admin.agents.critical")
                        : t("admin.agents.nonCritical")}
                    </Badge>
                  </CardTitle>
                  <CardDescription className="mt-1">{a.role}</CardDescription>
                </div>
                <Link href={`/admin/agents/${a.name}`}>
                  <Button variant="outline" size="sm">
                    <SettingsIcon className="h-4 w-4 mr-1" /> {t("admin.agents.editPrompt")}
                  </Button>
                </Link>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <RepoOverrides
                repos={overrides.data?.prompt_overrides?.[a.name] ?? []}
                loaded={!!overrides.data}
              />
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)] mb-1">
                  {t("admin.agents.whatItChecks")}
                </p>
                <ul className="text-sm space-y-1">
                  {a.focus.map((f, i) => (
                    <li key={i} className="flex gap-2">
                      <CheckCircle2Icon className="h-3.5 w-3.5 mt-0.5 opacity-60 flex-shrink-0" />
                      <span>{f}</span>
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted-foreground)] mb-1">
                  {t("admin.agents.contextReceived")}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {a.context_used.map((c, i) => (
                    <Badge key={i} variant="outline" className="text-[10px] font-normal">
                      {c}
                    </Badge>
                  ))}
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3 text-xs text-[var(--color-muted-foreground)] pt-2 border-t border-[var(--color-border)]">
                <div>
                  {t("admin.agents.defaultSeverity")} <code className="ml-1 font-semibold">{a.default_severity}</code>
                </div>
                {/* The field name alone was a dead end: it told you WHERE the
                    model comes from without saying where to change it, and the
                    output ceiling and reasoning setting live one layer up from
                    the repo policy, on the workspace LLM page. */}
                <div>
                  {t("admin.agents.modelConfig")} <code className="ml-1 font-semibold">policy.{a.settings_model_field}</code>
                  {" · "}
                  <Link className="underline" href="/settings/llm#review-agents">
                    {t("admin.agents.workspaceDefaultsLink")}
                  </Link>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </PageShell>
  );
}

/** "Overridden in N repositories" with a link to each one's policy page. */
function RepoOverrides({ repos, loaded }: {
  repos: Array<{ repo_slug: string }>;
  loaded: boolean;
}) {
  const t = useT();
  if (!loaded) return null;
  if (repos.length === 0) {
    return (
      <p className="text-xs text-[var(--color-muted-foreground)]">
        {t("admin.agents.noRepoOverrides")}
      </p>
    );
  }
  return (
    <div className="rounded-md border border-[var(--color-border)] p-2.5 text-xs">
      <p className="flex items-center gap-1.5 font-medium">
        <GitForkIcon className="h-3.5 w-3.5" />
        {t("admin.agents.overriddenInRepos", { count: repos.length })}
      </p>
      <p className="mt-0.5 text-[var(--color-muted-foreground)]">
        {t("admin.agents.overriddenInReposHint")}
      </p>
      <ul className="mt-1.5 flex flex-wrap gap-1.5">
        {repos.map((r) => (
          <li key={r.repo_slug}>
            <Link className="underline font-mono" href={repoPromptHref(r.repo_slug)}>
              {r.repo_slug}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
