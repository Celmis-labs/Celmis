"use client";

/**
 * Code review settings — every review setting, both layers, one page.
 *
 * Left: Global (the workspace defaults) and every repository, each with the
 * number of settings it overrides. Right: the open section of the open scope.
 * At Global a value is the workspace default; at a repository each field says
 * whether it is inherited (and from where, and what) or overridden, with a
 * reset beside every override. One Save for the whole scope, sticky, and
 * counting what it is about to change.
 *
 * It replaces three pages that each held a third of this — /admin/
 * review-defaults, /admin/review-policies (+ per repo) and /admin/agents
 * (+ per agent) — which are now redirects (lib/review-settings-routes.ts).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import {
  LockIcon, RotateCcwIcon, SaveIcon, SlidersHorizontalIcon, UndoIcon,
} from "lucide-react";

import {
  REVIEW_AGENTS,
  agentsApi,
  llmApi,
  reviewDefaultsApi,
  reviewPoliciesApi,
  reviewSettingsApi,
  type AgentSettings,
  type ModelCapabilities,
} from "@/lib/api";
import { globError, globLines } from "@/lib/ignore-globs";
import {
  sectionFromParam, settingsHref, type SectionId,
} from "@/lib/review-settings-routes";
import { useT } from "@/lib/i18n";
import { useCanEditPrompts } from "@/lib/use-analytics-access";
import { useToken } from "@/lib/use-token";
import {
  agentMaxOutError, useAgentCapabilities,
} from "@/components/agent-llm-controls";
import { PageHeader, PageShell } from "@/components/page-shell";
import { SectionTabs } from "@/components/section-tabs";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Skeleton, SkeletonRows } from "@/components/ui/skeleton";
import {
  SettingsContext, type AgentLLMState, type SettingsContextValue,
} from "@/components/review-settings/context";
import {
  GUIDELINES_MAX, POLICY_LLM_AGENTS, WORKSPACE_PROMPT_MIN, canonicalOwn, changedSections, defaultsPayload,
  draftFromDefaults, draftFromPolicy, emptyDraft, inheritanceForDefaults, inheritanceForPolicy,
  overriddenBySection, policyPayload, sameValue,
  type Draft, type InheritableKey, type Scope,
} from "@/components/review-settings/model";
import { ScopeNav, SECTION_ICON } from "@/components/review-settings/scope-nav";
import { GeneralSection } from "@/components/review-settings/section-general";
import { CategoriesSection } from "@/components/review-settings/section-categories";
import { FiltersSection } from "@/components/review-settings/section-filters";
import { PromptsSection } from "@/components/review-settings/section-prompts";
import { SummarySection } from "@/components/review-settings/section-summary";
import { RulesSection } from "@/components/review-settings/section-rules";
import { MessagesSection, messagesBlocked } from "@/components/review-settings/section-messages";
import { LearningSection } from "@/components/review-settings/section-learning";
import { AdvancedSection } from "@/components/review-settings/section-advanced";
import { CommandsSection } from "@/components/review-settings/section-commands";

const SECTION_VIEW: Record<SectionId, () => React.ReactNode> = {
  general: GeneralSection,
  categories: CategoriesSection,
  filters: FiltersSection,
  prompts: PromptsSection,
  summary: SummarySection,
  rules: RulesSection,
  messages: MessagesSection,
  commands: CommandsSection,
  learning: LearningSection,
  advanced: AdvancedSection,
};

type Loaded = { key: string; original: Draft; draft: Draft };

export function ReviewSettings() {
  const t = useT();
  const token = useToken();
  const qc = useQueryClient();
  const router = useRouter();
  const params = useSearchParams();
  const { confirm, dialog } = useConfirm();

  const repo = params.get("repo");
  const scope: Scope = useMemo(
    () => (repo ? { kind: "repo", slug: repo } : { kind: "workspace" }),
    [repo],
  );
  const section = sectionFromParam(params.get("section"), !!repo);
  const focusAgent = params.get("agent");
  const role = useCanEditPrompts();
  const canEditPrompts = role === true;

  // ── data ──
  const overview = useQuery({
    queryKey: ["review-settings", "overview"],
    queryFn: () => reviewSettingsApi.overview(token!),
    enabled: !!token,
  });
  // Read at both scopes: the vocabularies (choices, placeholders, languages)
  // and, at a repository, nothing else — the policy carries its inheritance.
  const defaults = useQuery({
    queryKey: ["review-defaults"],
    queryFn: () => reviewDefaultsApi.get(token!),
    enabled: !!token,
  });
  const policy = useQuery({
    queryKey: ["review-policies", "detail", repo],
    queryFn: () => reviewPoliciesApi.get(token!, repo!),
    enabled: !!token && !!repo,
  });
  const wsAgents = useQuery({
    queryKey: ["agents"],
    queryFn: () => agentsApi.list(token!),
    enabled: !!token,
  });
  const llmConfig = useQuery({
    queryKey: ["llm-config"],
    queryFn: () => llmApi.getConfig(token!),
    enabled: !!token,
  });

  const llmNames: string[] = scope.kind === "repo" ? [...POLICY_LLM_AGENTS] : [...REVIEW_AGENTS];

  // ── the draft: rebuilt from the server when the scope or the data changes,
  // never over unsaved typing ──
  const source = scope.kind === "repo" ? policy.data : defaults.data;
  const dataKey = source
    ? `${scope.kind}:${repo ?? ""}|${JSON.stringify(source)}|${
      scope.kind === "workspace"
        ? `${JSON.stringify(wsAgents.data ?? null)}|${JSON.stringify(defaults.data?.agents ?? null)}`
        : ""}`
    : "";
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const scopeOf = (key: string) => key.split("|")[0];
  const dirtyNow = loaded ? !sameDraft(loaded.draft, loaded.original, scope.kind) : false;
  if (dataKey && loaded?.key !== dataKey
      && (!dirtyNow || scopeOf(loaded?.key ?? "") !== scopeOf(dataKey))) {
    const fresh = scope.kind === "repo"
      ? draftFromPolicy(policy.data!)
      : draftFromDefaults(defaults.data!, wsAgents.data, defaults.data!.agents, llmNames);
    setLoaded({ key: dataKey, original: fresh, draft: fresh });
  }
  const ready = !!loaded && scopeOf(loaded.key) === scopeOf(dataKey);
  const draft = ready ? loaded!.draft : emptyDraft();
  const original = ready ? loaded!.original : emptyDraft();

  const inh = useMemo(() => {
    if (scope.kind === "repo") return policy.data ? inheritanceForPolicy(policy.data) : { values: {}, sources: {} };
    return defaults.data ? inheritanceForDefaults(defaults.data) : { values: {}, sources: {} };
  }, [scope.kind, policy.data, defaults.data]);

  const setDraft = (fn: (d: Draft) => Draft) =>
    setLoaded((prev) => (prev ? { ...prev, draft: fn(prev.draft) } : prev));
  const setOwn = (key: InheritableKey, value: unknown) =>
    setDraft((d) => ({ ...d, own: { ...d.own, [key]: value } }));
  const patch = (p: Partial<Draft>) => setDraft((d) => ({ ...d, ...p }));

  // ── per-agent models, at page level: Save has to refuse a ceiling above
  // what a model takes before the row that knows the ceiling has rendered ──
  const wsLLM = llmConfig.data?.agents as Record<string, AgentSettings> | undefined;
  const models = Object.fromEntries(llmNames.map((a) => [
    a, (draft.agentLLM[a]?.model ?? "").trim() || wsLLM?.[a]?.effective_model || "",
  ]));
  const capsList = useAgentCapabilities(llmNames.map((a) => models[a]));
  const caps = Object.fromEntries(llmNames.map((a, i) => [a, capsList[i]]));
  const capsOnly: Record<string, ModelCapabilities | null> = Object.fromEntries(
    llmNames.map((a, i) => [a, capsList[i]?.caps ?? null]),
  );
  const maxOutErrors = Object.fromEntries(llmNames.map((a, i) => [
    a, draft.agentLLM[a] ? agentMaxOutError(draft.agentLLM[a].maxOut, capsList[i]?.caps ?? null) : null,
  ]));
  const llm: AgentLLMState = {
    names: llmNames,
    models,
    caps,
    maxOutErrors,
    inherited: wsLLM,
    stored: scope.kind === "repo"
      ? (policy.data?.agent_llm_overrides ?? {})
      : (llmConfig.data?.agents ?? {}) as AgentLLMState["stored"],
    ready: !!llmConfig.data && ready,
  };

  // ── who may change what ──
  const canEditDefaults = defaults.data?.can_edit === true;
  const canEdit = scope.kind === "repo" ? canEditPrompts : canEditDefaults;

  // ── what changed, and what blocks a save ──
  const changed = useMemo(
    () => (ready ? changedSections(draft, original, scope.kind) : new Map<SectionId, number>()),
    [ready, draft, original, scope.kind],
  );
  const changeCount = [...changed.values()].reduce((a, b) => a + b, 0);
  const dirty = changeCount > 0;
  const placeholders = defaults.data?.message_placeholders ?? policy.data?.message_placeholders ?? [];
  const blockers: string[] = [];
  if (draft.own.ignore_globs && globError(globLines(String(draft.own.ignore_globs)))) {
    blockers.push(t("reviewSettings.save.blockedGlobs"));
  }
  if (Object.values(maxOutErrors).some((e) => e !== null)) blockers.push(t("settings.llm.agents.saveBlocked"));
  if (messagesBlocked(draft.own, placeholders)) blockers.push(t("reviewSettings.save.blockedMessages"));
  if (Object.entries(draft.workspacePrompts).some(([name, p]) => !p.reset
    && p.text !== wsAgents.data?.find((a) => a.name === name)?.system_prompt
    && p.text.trim().length < WORKSPACE_PROMPT_MIN)) {
    blockers.push(t("reviewSettings.save.blockedPrompt", { min: WORKSPACE_PROMPT_MIN }));
  }
  if (Object.values(draft.agentGuidelines).some((g) => g.trim().length > GUIDELINES_MAX)) {
    blockers.push(t("reviewSettings.save.blockedGuidelines", { max: GUIDELINES_MAX }));
  }

  // ── saving ──
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["review-settings"] });
    void qc.invalidateQueries({ queryKey: ["review-policies"] });
    void qc.invalidateQueries({ queryKey: ["review-defaults"] });
    void qc.invalidateQueries({ queryKey: ["llm-config"] });
    void qc.invalidateQueries({ queryKey: ["agents"] });
  };
  const markClean = () => setLoaded((prev) => (prev ? { ...prev, original: prev.draft } : prev));

  const save = useMutation({
    mutationFn: async () => {
      if (scope.kind === "repo") {
        await reviewPoliciesApi.upsert(token!, scope.slug, policyPayload(draft, policy.data!, capsOnly));
        return;
      }
      const agentsChanged = !sameValue(draft.agentLLM, original.agentLLM);
      const defaultsChanged = agentsChanged || !sameValue(
        canonicalOwn(draft.own, "workspace"), canonicalOwn(original.own, "workspace"));
      if (defaultsChanged && canEditDefaults) {
        await reviewDefaultsApi.save(token!, defaultsPayload(draft,
          agentsChanged && llmConfig.data
            ? { names: llmNames, stored: llmConfig.data.agents ?? {}, caps: capsOnly }
            : null));
      }
      if (canEditPrompts) {
        for (const a of wsAgents.data ?? []) {
          const p = draft.workspacePrompts[a.name];
          if (!p) continue;
          if (p.reset) await agentsApi.resetPrompt(token!, a.name);
          else if (p.text !== a.system_prompt) await agentsApi.overridePrompt(token!, a.name, p.text);
        }
        // Guidelines: what is ADDED to each prompt. Blank removes them.
        for (const a of wsAgents.data ?? []) {
          const next = (draft.agentGuidelines[a.name] ?? "").trim();
          if (next === (a.guidelines ?? "").trim()) continue;
          if (next) await agentsApi.setGuidelines(token!, a.name, next);
          else await agentsApi.resetGuidelines(token!, a.name);
        }
      }
    },
    onSuccess: () => {
      toast.success(t("reviewSettings.save.saved"));
      markClean();
      invalidate();
    },
    onError: (e) => toast.error(t("reviewSettings.save.failed", { message: (e as Error).message })),
  });

  const resetAll = useMutation({
    mutationFn: () => reviewPoliciesApi.reset(token!, (scope as { slug: string }).slug),
    onSuccess: () => {
      toast.success(t("reviewSettings.save.resetDone"));
      setLoaded(null);
      invalidate();
    },
    onError: (e) => toast.error(t("reviewSettings.save.failed", { message: (e as Error).message })),
  });

  const canSave = dirty && blockers.length === 0 && !save.isPending
    && (scope.kind === "repo" ? canEdit : canEditDefaults || canEditPrompts);

  // Ctrl/Cmd+S saves; leaving with unsaved changes asks first.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (canSave) save.mutate();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canSave, save]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const onNavigate = (e: React.MouseEvent, href: string, sameScope: boolean) => {
    if (sameScope || !dirty || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    void confirm({
      title: t("reviewSettings.save.leaveTitle"),
      description: t("reviewSettings.save.leaveBody", { count: changeCount }),
      confirmLabel: t("reviewSettings.save.discard"),
      danger: true,
    }).then((ok) => {
      if (!ok) return;
      setLoaded(null);
      router.push(href);
    });
  };

  const goTo = useCallback((s: SectionId) => {
    router.push(settingsHref({ repo, section: s }), { scroll: false });
  }, [router, repo]);

  const ctx: SettingsContextValue = {
    scope, draft, original, inh, setOwn, patch, canEdit,
    canEditPrompts,
    meta: {
      choices: defaults.data?.setting_choices ?? policy.data?.setting_choices ?? {},
      placeholders,
      languages: defaults.data?.review_languages ?? policy.data?.review_languages ?? [],
      participationDefaults: defaults.data?.agent_participation_defaults
        ?? policy.data?.agent_participation_defaults ?? {},
      overridableAgents: policy.data?.overridable_agents ?? [],
    },
    defaults: defaults.data,
    policy: policy.data,
    wsAgents: wsAgents.data,
    llm,
    focusAgent,
    goTo,
  };

  const repoSummary = repo ? overview.data?.repositories.find((r) => r.repo_slug === repo) : undefined;
  const unknownRepo = !!repo && !!overview.data && !repoSummary;
  const loadError = (scope.kind === "repo" ? policy.error : defaults.error) as Error | null;
  const View = SECTION_VIEW[section];
  const SectionIcon = SECTION_ICON[section];
  const activeCounts = ready ? overriddenBySection(draft, scope.kind) : new Map<SectionId, number>();
  const overriddenTotal = [...activeCounts.values()].reduce((a, b) => a + b, 0);

  const readOnlyReason = role === undefined || !ready
    ? null
    : scope.kind === "repo"
      ? (canEdit ? null : t("reviewSettings.access.repoReadOnly"))
      : canEditDefaults
        ? null
        : canEditPrompts
          ? t("reviewSettings.access.globalEditor")
          : t("reviewSettings.access.globalReadOnly");

  return (
    <PageShell width="wide">
      <PageHeader
        icon={<SlidersHorizontalIcon className="h-6 w-6" aria-hidden />}
        title={t("reviewSettings.title")}
        description={t("reviewSettings.description")}
        tabs={<SectionTabs set="review" />}
      />

      <div className="grid gap-6 lg:grid-cols-[15.5rem_minmax(0,1fr)] xl:grid-cols-[17rem_minmax(0,1fr)]">
        <aside className="lg:sticky lg:top-[calc(3.75rem+env(safe-area-inset-top))] lg:max-h-[calc(100dvh-5rem)] lg:self-start lg:overflow-y-auto lg:overscroll-contain lg:pr-1">
          <ScopeNav
            overview={overview.data}
            loading={overview.isLoading}
            scope={scope}
            section={section}
            activeCounts={activeCounts}
            changed={changed}
            onNavigate={onNavigate}
          />
        </aside>

        <div className="@container min-w-0 space-y-5">
          {/* The scope bar: where you are, what this scope overrides, and the
              one Save. Sticky under the app's top bar, so the button is in
              reach from the bottom of a long section. */}
          <div className="sticky top-[calc(2.75rem+env(safe-area-inset-top))] z-[5] -mx-1 flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] bg-[var(--color-background)]/95 px-1 py-3 backdrop-blur supports-[backdrop-filter]:bg-[var(--color-background)]/85">
            <div className="min-w-0">
              <p className="flex items-center gap-1.5 text-xs text-[var(--color-muted-foreground)]">
                {scope.kind === "repo" ? (
                  <span className="truncate font-mono">{scope.slug}</span>
                ) : (
                  <span>{t("reviewSettings.scope.global")}</span>
                )}
                <span aria-hidden>/</span>
                <span className="inline-flex items-center gap-1">
                  <SectionIcon className="h-3 w-3" aria-hidden />
                  {t(`reviewSettings.section.${section}`)}
                </span>
              </p>
              <p className="mt-0.5 text-sm">
                {!ready ? (
                  <Skeleton className="inline-block h-4 w-56 align-middle" />
                ) : scope.kind === "repo" ? (
                  overriddenTotal > 0
                    ? t("reviewSettings.scope.repoOverrides", { count: overriddenTotal })
                    : t("reviewSettings.scope.repoInheritsAll")
                ) : (
                  t("reviewSettings.scope.globalSummary", {
                    count: overriddenTotal,
                    repos: overview.data?.repositories.length ?? 0,
                  })
                )}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <AnimatePresence initial={false}>
                {dirty && (
                  <m.span
                    key="dirty"
                    initial={{ opacity: 0, x: 6 }}
                    animate={{ opacity: 1, x: 0 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.16, ease: [0.23, 1, 0.32, 1] }}
                    className="text-xs text-[var(--color-muted-foreground)]"
                    role="status"
                  >
                    {blockers[0]
                      ? <span className="text-[var(--color-destructive)]">{blockers[0]}</span>
                      : t("reviewSettings.save.unsaved", { count: changeCount })}
                  </m.span>
                )}
              </AnimatePresence>
              {dirty && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setLoaded((prev) => (prev ? { ...prev, draft: prev.original } : prev))}
                >
                  <UndoIcon className="h-3.5 w-3.5" aria-hidden />
                  {t("reviewSettings.save.discard")}
                </Button>
              )}
              {scope.kind === "repo" && repoSummary?.has_policy && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={!canEdit || resetAll.isPending}
                  onClick={async () => {
                    const ok = await confirm({
                      title: t("reviewSettings.save.resetAllTitle", { repo: scope.slug }),
                      description: t("reviewSettings.save.resetAllBody"),
                      confirmLabel: t("reviewSettings.save.resetAll"),
                      danger: true,
                    });
                    if (ok) resetAll.mutate();
                  }}
                >
                  <RotateCcwIcon className="h-3.5 w-3.5" aria-hidden />
                  {t("reviewSettings.save.resetAll")}
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                disabled={!canSave && !save.isPending}
                loading={save.isPending}
                aria-keyshortcuts="Control+S Meta+S"
                onClick={() => save.mutate()}
              >
                <SaveIcon className="h-3.5 w-3.5" aria-hidden />
                {save.isPending ? t("reviewSettings.save.saving") : t("reviewSettings.save.save")}
              </Button>
            </div>
          </div>

          {readOnlyReason && (
            <Callout tone="info" className="flex items-start gap-2">
              <LockIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>{readOnlyReason}</span>
            </Callout>
          )}
          {unknownRepo && (
            <Callout tone="warning">{t("reviewSettings.scope.unknownRepo", { repo: repo ?? "" })}</Callout>
          )}
          {loadError && !source && (
            <Callout tone="danger">{t("common.loadError")}: {loadError.message}</Callout>
          )}

          {!ready && !loadError ? (
            <SectionSkeleton />
          ) : ready ? (
            <SettingsContext.Provider value={ctx}>
              <m.div
                key={`${scope.kind}:${repo ?? ""}:${section}`}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.16, ease: "easeOut" }}
              >
                <View />
              </m.div>
            </SettingsContext.Provider>
          ) : null}
        </div>
      </div>
      {dialog}
    </PageShell>
  );
}

function sameDraft(a: Draft, b: Draft, scope: Scope["kind"]): boolean {
  return changedSections(a, b, scope).size === 0;
}

function SectionSkeleton() {
  return (
    <div className="space-y-6" aria-hidden>
      <div className="space-y-2">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-4 w-96 max-w-full" />
      </div>
      {[0, 1].map((g) => (
        <div key={g} className="space-y-2">
          <Skeleton className="h-4 w-32" />
          <div className="rounded-[var(--radius)] border border-[var(--color-border)] px-4">
            <SkeletonRows rows={3} />
          </div>
        </div>
      ))}
    </div>
  );
}
